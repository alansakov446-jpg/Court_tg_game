from aiogram import Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from db.models import Economy, Game, Player, utcnow
from game.rules import extend_deadline
from game.service import Court, change

router = Router()
HELP = """🏛 Суд — вымышленная судебная игра
В группе:
/game [0..9] — открыть зал; вступление кнопкой в ЛС
/extend 30 — продлить (предел 5 минут от создания)
/startnow, /stop — создатель зала или администратор
/players, /evidence, /balance — участники и материалы
/expert ID — экспертиза за деньги стороны
/publish ID — опубликовать свою скрытую улику
/call ID — вызвать свидетеля (адвокат/прокурор/судья)
/ask ID вопрос — допросить вызванного свидетеля
/object — протест; /objection accept|reject — решение судьи
/word ID — дать слово; /next — следующая фаза (судья)
/recuse ID — отвод; повтор команды другими игроками — поддержка
/return — вернуться после первого отстранения
/leave — навсегда передать роль боту после старта
/final текст — финальное слово подсудимого, до 200 символов
/vote guilty|innocent — голос присяжного (можно в ЛС)
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
    await query.answer()
    try:
        action, gid = (query.data or "").split(":")
        game = await court.db.get(Game, int(gid))
        if not game or game.status not in ("lobby", "running"):
            raise ValueError("Эта игра закрыта")
        p = await court.player(game, query.from_user.id)
        if action == "leave":
            await court.leave(game, p)
            await court.send(
                query.from_user.id,
                "Вы вышли из зала."
                if game.status == "lobby"
                else "Вас заменил бот навсегда.",
            )
        elif action == "object" and game.status == "running":
            await court.objection(game, p)
    except ValueError as exc:
        await court.send(query.from_user.id, str(exc))


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


async def handle(m, c):
    parts = m.text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    if "@" in parts[0] and parts[0].split("@", 1)[1].lower() != c.username.lower():
        return
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/help", "/start"):
        if cmd == "/start" and arg.startswith("join_"):
            return await c.join(m, arg)
        return await c.send(m.chat.id, HELP)
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
                m.chat.id, "Нет открытой игры. /game в группе или вступите по ссылке."
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
                "Зал суда👨‍⚖️ закрыли. Если хотите поиграть, просто напишите /game",
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
    if game.status != "running":
        return
    if cmd == "/players":
        lines = []
        for item in await c.players(game):
            r = await c.role(item)
            lines.append(
                f"{item.id}: {r.title} — {item.name}"
                + (" [бот]" if item.is_bot else "")
            )
        return await c.send(m.chat.id, "\n".join(lines))
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
        dest, text = arg.split(maxsplit=1)
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
        if (
            game.phase != "verdict"
            or not r.code.startswith("juror_")
            or arg not in ("guilty", "innocent")
        ):
            raise ValueError(
                "Присяжный голосует в фазе вердикта: /vote guilty|innocent"
            )
        change(game, votes={**game.state.get("votes", {}), str(p.id): arg})
        return await c.send(m.chat.id, "Голос записан.")
    if cmd == "/verdict":
        if r.code != "judge":
            raise ValueError("Вердикт выносит судья")
        return await c.verdict(game, arg)
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
    if cmd in ("/call", "/word", "/ask"):
        from datetime import timedelta

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
        if cmd in ("/call", "/ask") and (
            r.code not in ("judge", "prosecutor", "defense")
            or not tr.code.startswith("witness_")
        ):
            raise ValueError("Свидетеля вызывают судья, прокурор или адвокат")
        if cmd == "/ask":
            if game.state["speaker"] != tr.code or len(args) < 2:
                raise ValueError(
                    "Сначала вызовите свидетеля: /call ID, затем /ask ID вопрос"
                )
            await c.record(
                game, f"{r.title} спрашивает свидетеля: {args[1][:1500]}", ["public"], p
            )
        change(
            game,
            speaker=tr.code,
            questioner=r.code if r.code != "judge" else None,
            last_activity=utcnow().isoformat(),
            silence=0,
            bot_spoken=False,
            bot_due=(utcnow() + timedelta(seconds=3)).isoformat(),
        )
        return await c.public(game, f"Слово предоставлено: {tr.title}.")
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
                f"Отвод #{target.id}: поддержка {len(supporters)}/{threshold}. /recuse {target.id}",
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
        return await c.send(m.chat.id, "Неизвестная команда. /help")
    if game.phase == "final" and r.code == "accused":
        return await c.send(
            m.chat.id, "Для финального слова: /final текст (до 200 символов)."
        )
    if r.code != game.state["speaker"] and r.code != "judge":
        p.violations += 1
        await c.fine(game, p)
        if p.violations >= 2 and r.code != "accused":
            await c.remove(game, p)
        return
    if game.phase == "final":
        return  # Only /final is accepted during the protected final minute.
    await c.record(game, f"{r.title}: {m.text[:3000]}", ["public"], p)
    if r.code == game.state["speaker"]:
        change(game, last_activity=utcnow().isoformat(), silence=0)
