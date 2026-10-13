import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    Message,
)
from sqlalchemy import select

from db.models import Economy, Game, Player, utcnow
from game.rules import (
    commands_need_mention,
    extend_deadline,
    finish_phrase,
    route_command,
)
from game.service import Court, change

router = Router()

log = logging.getLogger("court.handlers")

# The command menu Telegram shows when a player types "/". Telegram accepts only
# lowercase latin names up to 32 characters, so every name here is a real
# command of this bot. Group and private scopes are registered separately: the
# hall lives in the group, the personal role, juror mail and the game picker
# live in the bot's private chat.
GROUP_COMMANDS = [
    ("game", "открыть зал суда: /game [0..9 ботов]"),
    ("startnow", "начать игру сразу, без ожидания"),
    ("lobby", "кто уже в зале суда"),
    ("players", "участники, номера и кому дано слово"),
    ("state", "кто говорит и сколько осталось"),
    ("extend", "продлить набор: /extend 30"),
    ("stop", "закрыть зал (создатель или админ)"),
    ("pass", "я всё — передать слово дальше"),
    ("done", "то же, что /pass"),
    ("ask", "вопрос ИИ-ведущему: /ask вопрос"),
    ("evidence", "улики: публичные в группе, свои в ЛС"),
    ("publish", "приобщить свою улику: /publish ID"),
    ("expert", "экспертиза улики: /expert ID"),
    ("balance", "бюджеты сторон и цена экспертизы"),
    ("call", "вызвать свидетеля: /call ID"),
    ("question", "допросить свидетеля: /question ID вопрос"),
    ("object", "протест против последней реплики"),
    ("objection", "решение судьи: /objection accept|reject"),
    ("word", "судья даёт слово: /word ID"),
    ("next", "судья завершает фазу: /next"),
    ("recuse", "отвод участника: /recuse ID"),
    ("return", "вернуться после первого отстранения"),
    ("leave", "выйти из зала или отдать роль боту"),
    ("final", "финальное слово подсудимого: /final текст"),
    ("vote", "голос присяжного: /vote guilty|innocent"),
    ("votes", "сколько присяжных уже проголосовало"),
    ("verdict", "решение судьи: /verdict guilty|innocent"),
    ("help", "все команды и правила"),
]
PRIVATE_COMMANDS = [
    ("games", "игры, в которых я участвую"),
    ("use", "выбрать игру: /use ID"),
    ("role", "моя роль, знания и улики"),
    ("lobby", "кто уже в зале суда"),
    ("state", "кто говорит и сколько осталось"),
    ("say", "присяжный — судье: /say anon|public текст"),
    ("sayto", "присяжный присяжному: /sayto ID текст"),
    ("vote", "тайный голос присяжного: /vote guilty|innocent"),
    ("votes", "сколько присяжных уже проголосовало"),
    ("help", "все команды и правила"),
]
COMMAND_SCOPES = (
    (BotCommandScopeAllGroupChats(), GROUP_COMMANDS),
    (BotCommandScopeAllPrivateChats(), PRIVATE_COMMANDS),
)


async def install_commands(bot, scopes=COMMAND_SCOPES):
    """Publish the "/" menu for groups and private chats.

    Telegram replaces the whole list at once, so a rejected description must not
    stop the bot: an empty menu is a cosmetic loss, a dead poller is not.
    """
    published = True
    for scope, commands in scopes:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in commands],
                scope=scope,
            )
            log.info(
                "Command menu published scope=%s commands=%d",
                type(scope).__name__,
                len(commands),
            )
        except Exception as exc:  # noqa: BLE001 — the menu is decoration, polling is not
            published = False
            log.warning(
                "Command menu not published scope=%s reason=%s",
                type(scope).__name__,
                type(exc).__name__,
            )
    return published
HELP = """🏛 Суд — вымышленная судебная игра
В группе:
/game [0..9] — открыть зал; вступление кнопкой в ЛС
/lobby — кто уже в зале (очередь вступления)
/extend 30 — продлить (предел 5 минут от создания)
/startnow, /stop — создатель зала или администратор
/players, /evidence, /balance — участники и материалы
/expert ID — экспертиза за деньги стороны
/publish ID — опубликовать свою скрытую улику
/ask вопрос — спросить ИИ-ведущего; ответ сохранится в памяти
/call ID — вызвать свидетеля (адвокат/прокурор/судья)
/question ID вопрос — допросить свидетеля (старый /ask ID вопрос тоже работает)
/pass или /done — сказать всё: слово сразу уходит следующей стороне
/object — протест; /objection accept|reject — решение судьи
/word ID — дать слово; /next — следующая фаза (судья)
/recuse ID — отвод; повтор команды другими игроками — поддержка
/return — вернуться после первого отстранения
/leave — навсегда передать роль боту после старта
/final текст — финальное слово подсудимого, до 200 символов
/vote guilty|innocent — голос присяжного (можно в ЛС)
/votes — сколько присяжных проголосовало (без выбора)
/verdict guilty|innocent — решение судьи
В ЛС:
/games, /use ID — выбрать игру при участии в нескольких
/role — своя роль и доступные знания
/say anon|public текст — присяжный пишет судье в ЛС
/sayto ID текст — личное сообщение другому присяжному
"""


async def manager(court, message, game):
    if message.from_user.id == game.creator_id:
        return True
    member = await court.bot.get_chat_member(game.chat_id, message.from_user.id)
    return member.status in ("creator", "administrator")


async def locate(court, message):
    if message.chat.type != "private":
        return await court.active_game(message.chat.id)
    games = list(
        (
            await court.db.scalars(
                select(Game)
                .join(Player, Player.game_id == Game.id)
                .where(
                    Player.user_id == message.from_user.id,
                    Player.active.is_(True),
                    Game.status.in_(["lobby", "running"]),
                )
                .order_by(Game.id.desc())
            )
        ).all()
    )
    selected = [
        g
        for g in games
        if g.state.get("dm_selection", {}).get(str(message.from_user.id))
    ]
    if selected:
        return selected[0]
    if len(games) > 1:
        raise ValueError("Вы участвуете в нескольких играх: /games, затем /use ID")
    return games[0] if games else None


@router.callback_query()
async def callback(query: CallbackQuery, court: Court):
    # Button data is action:game[:choice]; the answer is a private toast, so a
    # juror's choice is never echoed into the group.
    action, gid, choice = ((query.data or "").split(":") + ["", ""])[:3]

    async def toast(text, alert=False):
        """Telegram answers a button once, and an expired press must not raise."""
        try:
            await query.answer((text or "")[:190], show_alert=alert)
        except TelegramBadRequest:
            pass

    try:
        game = await court.db.get(Game, int(gid))
        if not game or game.status not in ("lobby", "running"):
            raise ValueError("Эта игра закрыта")
        p = await court.player(game, query.from_user.id)
        message = ""
        if action == "leave":
            await court.leave(game, p)
            message = (
                "Вы вышли из зала."
                if game.status == "lobby"
                else "Вас заменил бот навсегда."
            )
            await court.send(query.from_user.id, message)
            message = "Готово"
        elif action == "object" and game.status == "running":
            await court.objection(game, p)
            message = "Протест подан."
        elif action == "pass" and game.status == "running":
            # The player who holds the floor ends the turn instead of waiting
            # out the deadline; everyone else gets a local explanation.
            await court.hand_over(game, p)
            message = "Слово передано."
        elif action == "vote" and game.status == "running":
            message = await court.cast_vote(game, p, choice)
        elif action == "verdict" and game.status == "running":
            # Only the judge may press this button; the service verifies it.
            await court.verdict(game, choice, by=p)
            message = "Решение записано."
        await toast(message)
    except ValueError as exc:
        # A short toast is enough: the chat itself stays clean.
        await toast(str(exc), alert=True)


@router.message()
async def message_handler(message: Message, court: Court):
    if not message.from_user or message.from_user.is_bot or not message.text:
        return
    try:
        await handle(message, court)
    except (ValueError, IndexError) as exc:
        # ValueError messages below are all local, never provider URLs/credentials.
        text = (
            str(exc)
            if isinstance(exc, ValueError)
            and not str(exc).startswith("invalid literal")
            else "Проверьте аргументы команды: /help"
        )
        await court.send(message.chat.id, text or "Проверьте аргументы: /help")


def help_text(court, chat_type):
    """Help for the chat it is read in: in a group commands need the bot's name."""
    if chat_type != "private" and commands_need_mention():
        return (
            f"В группе команды адресуются боту: /game@{court.username}. "
            "Меню «/» подставляет имя само, а в ЛС имя не нужно.\n\n" + HELP
        )
    return HELP


async def handle(m, c):
    # In a group the bot reacts only to its own commands: a bare "/pass" or
    # somebody else's "/gg" gets no reply at all (see route_command).
    kind, cmd, arg = route_command(m.text, m.chat.type, c.username)
    if kind == "ignore":
        return
    if cmd in ("/help", "/start"):
        if cmd == "/start" and arg.startswith("join_"):
            return await c.join(m, arg)
        return await c.send(m.chat.id, help_text(c, m.chat.type))
    if cmd == "/game":
        return await c.new_game(m, int(arg) if arg else 0)
    if cmd in ("/games", "/use"):
        if m.chat.type != "private":
            raise ValueError("Эта команда работает в ЛС")
        games = list(
            (
                await c.db.scalars(
                    select(Game)
                    .join(Player, Player.game_id == Game.id)
                    .where(
                        Player.user_id == m.from_user.id,
                        Player.active.is_(True),
                        Game.status.in_(["lobby", "running"]),
                    )
                    .order_by(Game.id.desc())
                )
            ).all()
        )
        if cmd == "/games":
            return await c.send(
                m.chat.id,
                "Ваши залы:\n" + "\n".join(f"{g.id}: {g.title}" for g in games),
            )
        gid = int(arg)
        if not any(g.id == gid for g in games):
            raise ValueError("Вы не участвуете в этой игре")
        for g in games:
            choices = {**g.state.get("dm_selection", {})}
            choices[str(m.from_user.id)] = g.id == gid
            change(g, dm_selection=choices)
        return await c.send(m.chat.id, f"Выбрана игра #{gid}")
    game = await locate(c, m)
    if not game:
        if cmd.startswith("/"):
            await c.send(
                m.chat.id,
                "Нет открытой игры. "
                f"{c.cmd('game', group=m.chat.type != 'private')} в группе "
                "или вступите по ссылке.",
            )
        return
    p = await c.player(game, m.from_user.id)
    if cmd in ("/stop", "/startnow"):
        if m.chat.type == "private" or not await manager(c, m, game):
            raise ValueError("Команда доступна создателю или администратору в группе")
        if cmd == "/stop":
            game.status = "stopped"
            return await c.send(
                game.chat_id,
                "Зал суда👨‍⚖️ закрыли. Если хотите поиграть, просто напишите "
                f"{c.cmd('game')}",
            )
        return await c.start(game)
    if cmd == "/extend":
        if game.status != "lobby" or not p or not p.active:
            raise ValueError("Продлить лобби может вступивший игрок")
        game.deadline = extend_deadline(game.created_at, game.deadline, int(arg))
        return await c.send(
            game.chat_id,
            f"До старта {max(0, int((game.deadline - utcnow()).total_seconds()))} сек.",
        )
    if cmd == "/leave":
        return await c.leave(game, p)
    if cmd in ("/lobby", "/players") and game.status == "lobby":
        # Lobby: the roster is the answer; in the group the card is also refreshed.
        if m.chat.type != "private":
            await c.refresh_lobby(game)
        return await c.send(m.chat.id, await c.lobby_card(game))
    if game.status != "running":
        return
    if cmd == "/lobby":
        return await c.send(m.chat.id, await c.lobby_card(game, closing=True))
    if cmd == "/players":
        lines = []
        for item in await c.players(game):
            r = await c.role(item)
            lines.append(
                f"{item.id}: {r.title} — {item.name}"
                + (" [бот]" if item.is_bot else "")
                + (" • сейчас говорит" if r.code == game.state.get("speaker") else "")
            )
        return await c.send(m.chat.id, "\n".join(lines))
    if cmd == "/state":
        # Anyone in the chat may ask who is speaking; no role is required.
        return await c.send(m.chat.id, await c.turn_state_text(game))
    if cmd == "/balance":
        wallets = (
            await c.db.scalars(select(Economy).where(Economy.game_id == game.id))
        ).all()
        return await c.send(
            m.chat.id,
            "\n".join(f"{w.side}: {w.balance}$" for w in wallets)
            + f"\nЭкспертиза: {game.state['price']}$",
        )
    if cmd == "/evidence" and m.chat.type != "private":
        return await c.show_evidence(game, m.chat.id)
    if not p or not p.active:
        return
    r = await c.role(p)
    if cmd == "/return":
        if p.permanent or not p.is_bot:
            raise ValueError(
                "Возврат недоступен: добровольный выход, повторное отстранение или вы уже играете"
            )
        p.is_bot = False
        return await c.public(game, f"{p.name} вернулся в роль {r.title}.")
    if p.is_bot:
        if cmd.startswith("/"):
            raise ValueError("Вашей ролью управляет бот")
        return
    if cmd == "/ask":
        # Backward compatibility: /ask ID вопрос still interrogates a real
        # witness in the group. Every other /ask is a question to the AI host.
        args = arg.split(maxsplit=1)
        target = (
            await c.db.get(Player, int(args[0]))
            if m.chat.type != "private" and args and args[0].isdigit()
            else None
        )
        target_role = await c.role(target) if target else None
        if (
            target
            and target.game_id == game.id
            and target.active
            and target_role
            and target_role.code.startswith("witness_")
        ):
            cmd = "/question"
        else:
            return await c.ask_host(
                game,
                p,
                arg,
                m.chat.id,
                public=m.chat.type != "private",
            )
    if cmd == "/role":
        context = await c.context(game, r)
        await c.send(
            p.user_id,
            f"{game.title}: {r.title}, номер {p.id}\n"
            + "\n".join(context["memory"])[-3000:],
        )
        return await c.show_evidence(game, p.user_id, r)
    if cmd == "/evidence":
        return await c.show_evidence(game, m.chat.id, r)
    if cmd in ("/say", "/sayto"):
        if m.chat.type != "private" or not r.code.startswith("juror_"):
            raise ValueError("Переписка присяжных работает в ЛС бота")
        # A missing text used to crash on unpacking; now it is a clear hint.
        dest, text = (arg.split(maxsplit=1) + [""])[:2]
        if not dest or not text:
            raise ValueError("/say anon|public текст или /sayto ID текст")
        if len(text) > 1500:
            raise ValueError("Сообщение до 1500 символов")
        if cmd == "/say":
            if dest not in ("anon", "public"):
                raise ValueError("/say anon|public текст")
            target = await c.by_code(game, "judge")
            author = "Анонимный присяжный" if dest == "anon" else p.name
        else:
            target = await c.db.get(Player, int(dest))
            tr = await c.role(target)
            if (
                not target
                or target.game_id != game.id
                or not tr
                or not tr.code.startswith("juror_")
            ):
                raise ValueError("Адресат должен быть присяжным этой игры")
            author = p.name
        tr = await c.role(target)
        content = f"{author}: {text}"
        await c.record(game, content, [r.code, tr.code])
        if not target.is_bot:
            await c.send(target.user_id, f"Зал {game.title}\n{content}")
        return await c.send(m.chat.id, "Сообщение передано.")
    if cmd == "/vote":
        return await c.send(m.chat.id, await c.cast_vote(game, p, arg))
    if cmd == "/votes":
        if game.phase != "verdict":
            raise ValueError("Голосование присяжных идёт в фазе вердикта")
        return await c.send(m.chat.id, await c.vote_progress(game))
    if cmd in ("/pass", "/done"):
        # "Я всё сказал": the floor moves on at once, no waiting for the deadline.
        return await c.hand_over(game, p)
    if cmd == "/verdict":
        return await c.verdict(game, arg, by=p)
    # Other public procedural commands must not expose private text unexpectedly.
    if m.chat.type == "private":
        if cmd.startswith("/"):
            await c.send(m.chat.id, "Эта команда выполняется в группе. /help")
        return
    if cmd in ("/expert", "/publish"):
        from db.models import Evidence

        e = await c.db.get(Evidence, int(arg))
        if (
            not e
            or e.game_id != game.id
            or not set(e.visible_to_roles).intersection(["public", r.code])
        ):
            raise ValueError("Эта улика вам недоступна")
        if cmd == "/expert":
            if not r.side:
                raise ValueError("Экспертизу оплачивает сторона обвинения или защиты")
            await c.charge(game, r.side, game.state["price"])
            return await c.public(
                game, f"Экспертиза #{e.id} ({game.state['price']}$): {e.interpretation}"
            )
        e.visible_to_roles = list(set(e.visible_to_roles + ["public"]))
        return await c.public(game, f"Приобщено: #{e.id} {e.title}: {e.content}")
    if cmd in ("/call", "/word", "/question"):
        args = arg.split(maxsplit=1)
        target = await c.db.get(Player, int(args[0]))
        tr = await c.role(target)
        if not target or target.game_id != game.id or not target.active or not tr:
            raise ValueError("Нет такого участника в этой игре")
        if game.phase not in ("accusation", "defense", "debate"):
            raise ValueError(
                "В этой фазе нельзя передавать слово или вызывать свидетеля"
            )
        if cmd == "/word" and r.code != "judge":
            raise ValueError("Слово предоставляет судья")
        if cmd in ("/call", "/question") and (
            r.code not in ("judge", "prosecutor", "defense")
            or not tr.code.startswith("witness_")
        ):
            raise ValueError("Свидетеля вызывают судья, прокурор или адвокат")
        question = args[1][:1500] if cmd == "/question" and len(args) > 1 else ""
        if cmd == "/question":
            if game.state.get("speaker") != tr.code or not question:
                raise ValueError(
                    "Сначала вызовите свидетеля: "
                    f"{c.cmd('call')} ID, затем {c.cmd('question')} ID вопрос"
                )
            await c.record(
                game, f"{r.title} спрашивает свидетеля: {question}", ["public"], p
            )
        if cmd == "/call":
            await c.public(
                game,
                f"{r.title} вызывает {tr.title}."
                + (f" Вопрос: {question}" if question else ""),
            )
        # The witness is pinged and, if it is a human, gets the «я всё» button
        # right away; a bot witness answers a few seconds later on its own.
        return await c.give_floor(
            game,
            tr.code,
            by=r,
            note=f"Вопрос: {question}" if question else "",
            delay=3,
            questioner=r.code if r.code != "judge" else "",
            handover=False,
        )
    if cmd == "/next":
        if r.code != "judge" or game.phase in ("final", "verdict"):
            raise ValueError(
                "Судья может завершить текущую фазу, но не пропустить финальное слово или вердикт"
            )
        return await c.advance(game)
    if cmd == "/object":
        return await c.objection(game, p)
    if cmd == "/objection":
        if r.code != "judge" or arg not in ("accept", "reject"):
            raise ValueError("Судья: /objection accept|reject")
        return await c.resolve_objection(game, arg == "accept")
    if cmd == "/recuse":
        target = await c.db.get(Player, int(arg))
        tr = await c.role(target)
        if (
            not target
            or target.game_id != game.id
            or not target.active
            or target.is_bot
            or target.id == p.id
            or tr.code == "accused"
        ):
            raise ValueError(
                "Нельзя отвести себя, подсудимого, бота или участника другой игры"
            )
        motions = {**game.state.get("motions", {})}
        supporters = list(dict.fromkeys(motions.get(str(target.id), []) + [p.id]))
        motions[str(target.id)] = supporters
        change(game, motions=motions)
        humans = [
            x for x in await c.players(game) if not x.is_bot and x.id != target.id
        ]
        threshold = len(humans) // 2 + 1
        if len(set(supporters).intersection(x.id for x in humans)) >= threshold:
            await c.remove(game, target, await c.db.get(Player, supporters[0]))
            motions.pop(str(target.id), None)
            change(game, motions=motions)
        else:
            await c.public(
                game,
                f"Отвод #{target.id}: поддержка {len(supporters)}/{threshold}. "
                f"{c.cmd('recuse')} {target.id}",
            )
        return
    if cmd == "/final":
        if (
            r.code != "accused"
            or game.phase != "final"
            or game.state.get("final_spoken")
        ):
            raise ValueError(
                "Финальное слово доступно подсудимому один раз в своей фазе"
            )
        if not arg or len(arg) > 200:
            raise ValueError("Финальное слово: от 1 до 200 символов")
        change(game, final_spoken=True)
        await c.record(game, f"Финальное слово: {arg}", ["public"], p)
        return await c.public(game, f"Финальное слово подсудимого: {arg}")
    if cmd.startswith("/"):
        # Command names are shown the way the bot will hear them in this chat.
        return await c.send(
            m.chat.id,
            f"Неизвестная команда. {c.cmd('help', group=m.chat.type != 'private')}",
        )
    if r.code == "accused" and game.phase == "final":
        return await c.send(
            m.chat.id,
            "Вы уже сказали финальное слово. Закончили — напишите «я всё» "
            "или нажмите кнопку, и суд перейдёт к вердикту."
            if game.state.get("final_spoken")
            else (
                f"Для финального слова: {c.cmd('final')} текст "
                "(до 200 символов). Закончили — напишите «я всё»."
            ),
        )
    # «Я всё», «на этом у меня всё»: the speaker ends the turn, the court hands
    # the word over at once. Anyone else falls through to the usual penalty path.
    if r.code in (game.state.get("speaker"), "judge") and finish_phrase(m.text):
        try:
            return await c.hand_over(game, p)
        except ValueError as exc:
            return await c.send(m.chat.id, str(exc))
    if r.code != game.state.get("speaker") and r.code != "judge":
        p.violations += 1
        await c.fine(game, p)
        if p.violations >= 2 and r.code != "accused":
            await c.remove(game, p)
        return
    await c.record(game, f"{r.title}: {m.text[:3000]}", ["public"], p)
    if r.code == game.state.get("speaker"):
        change(game, last_activity=utcnow().isoformat(), silence=0)
