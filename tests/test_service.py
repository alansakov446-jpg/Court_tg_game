import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

from sqlalchemy.dialects import postgresql

from ai.gemini import AIUnavailable
from db.models import Game, Player, Role, utcnow
from game.rules import SPEAKERS
from game.service import MAX_HANDOVERS, Court, change

# Russian role titles used by the handover tests.
TITLES = {
    "judge": "Судья",
    "prosecutor": "Прокурор",
    "defense": "Адвокат",
    "accused": "Подсудимый",
    "witness_1": "Свидетель 1",
}
assert set(SPEAKERS.values()) <= set(TITLES)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.flush = AsyncMock()
        self.db.scalar = AsyncMock(return_value=None)
        self.db.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        self.bot = SimpleNamespace(
            send_message=AsyncMock(), edit_message_text=AsyncMock()
        )
        self.court = Court(self.db, self.bot, MagicMock(), "court_bot")
        self.game = Game(
            id=1,
            chat_id=-100,
            title="test",
            status="running",
            phase="defense",
            state={},
            deadline=utcnow() + timedelta(seconds=150),
        )
        self.p = Player(
            id=2,
            game_id=1,
            user_id=123,
            name="Игрок",
            active=True,
            is_bot=False,
            permanent=False,
            removals=0,
            violations=0,
        )
        self.role = Role(code="defense", title="Адвокат", side="defense")
        self.court.role = AsyncMock(return_value=self.role)

    async def test_lobby_exit_is_reversible(self):
        self.game.status = "lobby"
        await self.court.leave(self.game, self.p)
        self.assertFalse(self.p.active)
        self.assertFalse(self.p.permanent)
        self.assertFalse(self.p.is_bot)

    async def test_voluntary_running_exit_is_permanent(self):
        await self.court.leave(self.game, self.p)
        self.assertTrue(self.p.is_bot)
        self.assertTrue(self.p.permanent)
        self.assertTrue(self.p.active)

    async def test_second_removal_is_permanent(self):
        await self.court.remove(self.game, self.p)
        self.assertTrue(self.p.is_bot)
        self.assertFalse(self.p.permanent)
        self.p.is_bot = False
        await self.court.remove(self.game, self.p)
        self.assertTrue(self.p.permanent)

    async def test_cannot_remove_accused_or_self(self):
        self.role.code = "accused"
        with self.assertRaises(ValueError):
            await self.court.remove(self.game, self.p)
        self.role.code = "defense"
        with self.assertRaises(ValueError):
            await self.court.remove(self.game, self.p, self.p)

    async def test_knowledge_filtered_in_sql_and_no_hidden_interpretation(self):
        queries = []
        clue = SimpleNamespace(
            id=1, title="Нож", content="След", interpretation="SECRET"
        )
        statement = SimpleNamespace(content="Разрешённая память")

        async def scalars(query):
            queries.append(query)
            result = MagicMock()
            result.all.return_value = [clue] if len(queries) == 1 else [statement]
            return result

        self.db.scalars = scalars
        self.game.state = {"truth": "TOP_SECRET"}
        ctx = await self.court.context(self.game, self.role)
        self.assertNotIn("SECRET", str(ctx))
        for query in queries:
            sql = str(
                query.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            self.assertIn("visible_to_roles &&", sql)
            self.assertIn("public", sql)
            self.assertIn("defense", sql)
            self.assertIn("game_id = 1", sql)

    async def test_private_host_answer_is_saved_in_role_memory_for_replacement(self):
        self.court.context = AsyncMock(return_value={"memory": ["Известный факт"]})
        self.court.record = AsyncMock()
        with patch(
            "game.service.answer_question",
            AsyncMock(return_value="Событие произошло на складе."),
        ) as ask:
            await self.court.ask_host(
                self.game,
                self.p,
                "Где это произошло?",
                self.p.user_id,
                public=False,
            )
        self.court.context.assert_awaited_once_with(self.game, self.role)
        ask.assert_awaited_once_with(
            self.court.ai,
            self.role.title,
            "Где это произошло?",
            {"memory": ["Известный факт"]},
        )
        saved = self.court.record.await_args.args
        self.assertEqual(saved[0], self.game)
        self.assertIn("Где это произошло?", saved[1])
        self.assertIn("Событие произошло на складе.", saved[1])
        # context() authorizes by role code, so the bot taking over this same
        # Player/Role reads the persisted answer without a separate memory table.
        self.assertEqual(saved[2], ["defense"])
        self.assertIs(saved[3], self.p)
        sent = self.bot.send_message.await_args.args[1]
        self.assertIn("сохранён в памяти роли", sent)

    async def test_public_host_question_uses_only_public_context(self):
        self.court.context = AsyncMock(return_value={"memory": ["Общая реплика"]})
        self.court.record = AsyncMock()
        with patch(
            "game.service.answer_question", AsyncMock(return_value="Это пока не указано.")
        ):
            await self.court.ask_host(
                self.game,
                self.p,
                "Какое было орудие?",
                self.game.chat_id,
                public=True,
            )
        self.court.context.assert_awaited_once_with(self.game, None)
        self.assertEqual(self.court.record.await_args.args[2], ["public"])
        self.assertIn("сохранён в общей памяти", self.bot.send_message.await_args.args[1])

    async def test_host_failure_is_reported_without_saving_false_memory(self):
        self.court.context = AsyncMock(return_value={})
        self.court.record = AsyncMock()
        failure = AIUnavailable("нет", summary="http=429")
        with (
            patch("game.service.answer_question", AsyncMock(side_effect=failure)),
            self.assertLogs("court.game", level="WARNING") as logs,
            self.assertRaisesRegex(ValueError, "временно не ответил"),
        ):
            await self.court.ask_host(
                self.game, self.p, "Кто был рядом?", self.p.user_id
            )
        self.court.record.assert_not_awaited()
        output = "\n".join(logs.output)
        self.assertIn("Host answer unavailable game=1 role=defense", output)
        self.assertNotIn("Кто был рядом", output)
        self.assertNotIn("Игрок", output)

    async def test_insufficient_balance_does_not_charge(self):
        wallet = SimpleNamespace(balance=10)
        self.db.scalar = AsyncMock(return_value=wallet)
        with self.assertRaises(ValueError):
            await self.court.charge(self.game, "defense", 25)
        self.assertEqual(wallet.balance, 10)

    async def test_tick_advances_from_defense_to_debate(self):
        self.game.deadline = utcnow() - timedelta(seconds=1)
        self.court.set_phase = AsyncMock()
        await self.court.tick(self.game)
        self.court.set_phase.assert_awaited_once_with(self.game, "debate")

    async def test_unanimous_jury_grants_reconsideration(self):
        self.game.phase = "verdict"
        self.court.vote_summary = AsyncMock(
            return_value=(
                [],
                {"1": "innocent", "2": "innocent", "3": "innocent"},
                True,
                True,
            )
        )
        self.court.finish = AsyncMock()
        await self.court.verdict(self.game, "guilty")
        self.assertTrue(self.game.state["appealed"])
        self.court.finish.assert_not_awaited()
        await self.court.verdict(self.game, "innocent")
        self.court.finish.assert_awaited_once_with(self.game, "innocent")

    async def test_case_failure_uses_fallback_template_and_starts(self):
        self.game.status = "lobby"
        self.court.players = AsyncMock(return_value=[])
        empty = MagicMock(all=MagicMock(return_value=[]))
        self.db.scalars = AsyncMock(return_value=empty)
        self.db.flush = AsyncMock()
        failure = AIUnavailable("ИИ временно недоступен", summary="http=429 x10")
        with (
            patch("game.service.create_case", AsyncMock(side_effect=failure)),
            self.assertLogs("court.game", level="ERROR") as logs,
        ):
            await self.court.start(self.game)
        output = "\n".join(logs.output)
        self.assertIn("New case was not created game=1 players=0", output)
        self.assertIn("AIUnavailable: http=429 x10", output)
        self.assertIn("fallback template used", output)
        self.assertNotIn("Игрок", output)  # names and chat text stay out of the logs
        # The room must start even with the AI down: deterministic template case.
        self.assertEqual(self.game.status, "running")
        self.assertIn("Дело №1", self.game.state["crime"])
        self.assertEqual(self.game.state["truth"], "innocent")
        messages = [str(call.args[1]) for call in self.bot.send_message.await_args_list]
        chat = "\n".join(messages)
        self.assertIn("стандартному делу из шаблона", chat)
        self.assertIn("квоты ИИ исчерпаны", chat)  # the specific reason is told to players

    async def test_bot_speech_fallback_is_logged_without_player_data(self):
        self.game.phase = "debate"
        self.court.context = AsyncMock(return_value={})
        self.court.record = AsyncMock()
        self.court.bot_action = AsyncMock()
        with (
            patch("game.service.speak", AsyncMock(side_effect=TimeoutError())),
            self.assertLogs("court.game", level="WARNING") as logs,
        ):
            await self.court.bot_turn(self.game, self.p)
        output = "\n".join(logs.output)
        self.assertIn("Bot speech fallback game=1 phase=debate role=defense", output)
        self.assertIn("TimeoutError", output)
        self.assertNotIn("Игрок", output)  # names and chat text stay out of the logs

    async def test_ai_vote_fallback_is_logged(self):
        self.court.context = AsyncMock(return_value={})
        self.court.ai.generate = AsyncMock(
            side_effect=AIUnavailable("нет", summary="timeout=ReadTimeout x2")
        )
        with self.assertLogs("court.game", level="WARNING") as logs:
            vote = await self.court.ai_decision(self.game, self.p)
        self.assertEqual(vote, "innocent")
        output = "\n".join(logs.output)
        self.assertIn("AI vote fallback game=1 role=defense", output)
        self.assertIn("timeout=ReadTimeout x2", output)

    # --- Handover: "я всё" for players, the "done" signal for bots ---------

    def speaking(self, code, role_code=None, receiver=None):
        """The state as it looks while `code` holds the floor.

        `role_code` is the role of the acting player (self.p) when it differs
        from the speaker; `receiver` is what by_code() returns for a handover.
        """
        self.role.code = role_code or code
        self.role.title = TITLES[self.role.code]
        change(self.game, **self.court.turn_state(self.game, code))
        self.court.by_code = AsyncMock(
            return_value=receiver if receiver is not None else self.p
        )

    async def test_speaker_finishes_own_phase_and_the_next_phase_starts(self):
        self.game.phase = "defense"
        self.speaking("defense")
        self.court.set_phase = AsyncMock()
        await self.court.hand_over(self.game, self.p)
        self.court.set_phase.assert_awaited_once_with(self.game, "debate")

    async def test_debate_handover_reaches_the_other_side_instantly(self):
        other = Player(
            id=9, game_id=1, user_id=77, name="Прокурор", active=True, is_bot=False
        )
        self.game.phase = "debate"
        self.speaking("defense", receiver=other)
        await self.court.hand_over(self.game, self.p)
        self.assertEqual(self.game.state["speaker"], "prosecutor")
        text = self.bot.send_message.await_args.args[1]
        kwargs = self.bot.send_message.await_args.kwargs
        self.assertIn("передаёт слово", text)
        self.assertIn('href="tg://user?id=77"', text)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("pass:1", str(kwargs["reply_markup"]))

    async def test_witness_handover_returns_to_the_questioner(self):
        witness = Player(id=7, game_id=1, name="Свидетель", active=True, is_bot=False)
        self.game.phase = "accusation"
        self.speaking("witness_1", receiver=witness)
        change(self.game, questioner="prosecutor")
        await self.court.hand_over(self.game, self.p)
        self.assertEqual(self.game.state["speaker"], "prosecutor")
        self.assertEqual(self.court.by_code.await_args.args[1], "prosecutor")

    async def test_only_the_speaker_can_hand_the_word_over(self):
        self.game.phase = "debate"
        self.speaking("defense", role_code="prosecutor")
        with self.assertRaisesRegex(ValueError, "другая роль"):
            await self.court.hand_over(self.game, self.p)

    async def test_back_to_back_handovers_are_refused(self):
        other = Player(id=9, game_id=1, user_id=77, name="Прокурор", active=True)
        self.game.phase = "debate"
        self.speaking("defense", receiver=other)
        await self.court.hand_over(self.game, self.p)
        self.speaking("prosecutor", receiver=self.p)
        with self.assertRaisesRegex(ValueError, "только что передавали"):
            await self.court.hand_over(self.game, self.p)

    async def test_handover_limit_stops_an_endless_exchange(self):
        other = Player(id=9, game_id=1, user_id=77, name="Прокурор", active=True)
        self.game.phase = "debate"
        self.speaking("defense", receiver=other)
        change(self.game, handovers=MAX_HANDOVERS)
        with self.assertRaisesRegex(ValueError, "слишком часто"):
            await self.court.hand_over(self.game, self.p)

    async def test_bot_holdover_requires_the_bot_signal(self):
        self.game.phase = "debate"
        self.p.is_bot = True
        self.speaking("defense")
        with self.assertRaisesRegex(ValueError, "управляет бот"):
            await self.court.hand_over(self.game, self.p)
        self.court.by_code = AsyncMock(return_value=None)
        with self.assertRaisesRegex(ValueError, "Некуда передавать"):
            await self.court.hand_over(self.game, self.p, allow_bot=True)

    async def test_accused_needs_the_final_word_before_closing_it(self):
        self.game.phase = "final"
        self.speaking("accused")
        with self.assertRaisesRegex(ValueError, "финальное слово"):
            await self.court.hand_over(self.game, self.p)
        change(self.game, final_spoken=True)
        self.court.advance = AsyncMock()
        await self.court.hand_over(self.game, self.p)
        self.court.advance.assert_awaited_once_with(self.game)

    async def test_judge_ends_the_phase_but_never_the_final_word(self):
        self.game.phase = "accusation"
        self.speaking("judge")
        self.court.advance = AsyncMock()
        await self.court.hand_over(self.game, self.p)
        self.court.advance.assert_awaited_once_with(self.game)
        self.game.phase = "final"
        with self.assertRaisesRegex(ValueError, "финальное слово"):
            await self.court.hand_over(self.game, self.p)

    async def test_crime_and_verdict_phases_have_no_handover(self):
        for phase in ("crime", "verdict"):
            self.game.phase = phase
            self.speaking("defense")
            with self.assertRaisesRegex(ValueError, "ход ведёт суд"):
                await self.court.hand_over(self.game, self.p)

    async def test_bot_signal_hands_the_floor_over_at_once(self):
        other = Player(id=9, game_id=1, user_id=77, name="Прокурор", active=True)
        self.p.is_bot = True
        self.game.phase = "defense"
        self.speaking("defense", receiver=other)
        self.court.bot_action = AsyncMock()
        self.court.context = AsyncMock(return_value={})
        with patch("game.service.speak", AsyncMock(return_value=("Речь", True))):
            await self.court.bot_turn(self.game, self.p)
        self.assertEqual(self.game.state["speaker"], "prosecutor")
        self.assertEqual(
            self.court.by_code.await_args.args, (self.game, "prosecutor")
        )

    async def test_bot_without_the_signal_keeps_the_floor(self):
        self.p.is_bot = True
        self.game.phase = "defense"
        self.speaking("defense")
        self.court.bot_action = AsyncMock()
        self.court.context = AsyncMock(return_value={})
        with patch("game.service.speak", AsyncMock(return_value=("Речь", False))):
            await self.court.bot_turn(self.game, self.p)
        self.assertEqual(self.game.state["speaker"], "defense")
        self.assertTrue(self.game.state["bot_spoken"])

    async def test_bot_speech_carries_the_done_flag_from_the_model(self):
        self.court.context = AsyncMock(return_value={})
        ai = MagicMock()
        ai.generate = AsyncMock(return_value={"speech": "Мы всё доказали.", "done": True})
        self.court.ai = ai
        text, done = await self.court.bot_speech(self.game, self.role)
        self.assertEqual(text, "Мы всё доказали.")
        self.assertTrue(done)
        self.assertIn('"done"', ai.generate.await_args.args[0])

    async def test_bot_speech_failure_keeps_the_turn_open(self):
        self.court.context = AsyncMock(return_value={})
        ai = MagicMock()
        ai.generate = AsyncMock(return_value={"done": True})
        self.court.ai = ai
        with self.assertLogs(level="WARNING") as logs:
            text, done = await self.court.bot_speech(self.game, self.role)
        self.assertIn("Новых сведений у меня нет", text)
        self.assertFalse(done)
        self.assertIn("Bot speech fallback game=1 phase=defense role=defense", "\n".join(logs.output))

    # --- Lobby roster ------------------------------------------------------

    async def test_lobby_card_lists_everyone_already_in_the_hall(self):
        self.game.status = "lobby"
        self.game.state = {"requested_bots": 2, "lobby_message": 77}
        people = [
            Player(id=1, game_id=1, user_id=5, name="Аня", active=True),
            Player(id=2, game_id=1, name="Бот 1", active=True, is_bot=True),
        ]
        self.court.players = AsyncMock(return_value=people)
        card = await self.court.lobby_card(self.game)
        self.assertIn("Зал суда «test»", card)
        self.assertIn("👥 В зале (2):", card)
        self.assertIn("1. Аня", card)
        self.assertIn("2. Бот 1 [бот]", card)
        self.assertIn("Свободных мест: 7", card)
        self.assertIn("До старта:", card)

    async def test_lobby_card_is_edited_in_place_not_reposted(self):
        self.game.status = "lobby"
        self.game.state = {"lobby_message": 77}
        self.court.players = AsyncMock(return_value=[])
        await self.court.refresh_lobby(self.game)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 77)
        self.bot.send_message.assert_not_awaited()

    async def test_lobby_card_is_reposted_when_the_edit_fails(self):
        self.game.status = "lobby"
        self.game.state = {"lobby_message": 77}
        self.court.players = AsyncMock(return_value=[])
        self.bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method=None, message="message to edit not found")
        )
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=88))
        await self.court.refresh_lobby(self.game)
        self.assertEqual(self.game.state["lobby_message"], 88)

    async def test_lobby_card_closes_without_a_join_button(self):
        self.game.status = "lobby"
        self.game.state = {"lobby_message": 77}
        self.court.players = AsyncMock(return_value=[])
        await self.court.refresh_lobby(self.game, closing=True)
        self.assertIn("Набор закрыт", self.bot.edit_message_text.await_args.kwargs["text"])
        self.assertIsNone(
            self.bot.edit_message_text.await_args.kwargs["reply_markup"]
        )

    async def test_leaving_the_lobby_updates_the_card(self):
        self.game.status = "lobby"
        self.game.state = {}
        self.court.players = AsyncMock(return_value=[])
        await self.court.leave(self.game, self.p)
        self.assertFalse(self.p.active)
        self.assertIn("👥 В зале (0)", self.bot.send_message.await_args.args[1])

    async def test_lobby_countdown_notice_shows_the_roster(self):
        self.game.status = "lobby"
        self.game.state = {}
        self.game.last_notice = utcnow() - timedelta(seconds=31)
        self.court.players = AsyncMock(
            return_value=[Player(id=1, game_id=1, name="Аня", active=True)]
        )
        self.court.refresh_lobby = AsyncMock()
        await self.court.tick(self.game)
        self.assertIn("Аня", self.bot.send_message.await_args.args[1])
        self.court.refresh_lobby.assert_awaited_once()

    # --- Voting progress, mentions, messages -------------------------------

    async def test_vote_progress_names_who_is_late_without_revealing_votes(self):
        jury = [
            Player(id=1, game_id=1, name="Аня", active=True),
            Player(id=2, game_id=1, name="Боб", active=True),
        ]
        self.court.vote_summary = AsyncMock(return_value=(jury, {"1": "guilty"}, False, False))
        progress = await self.court.vote_progress(self.game)
        self.assertIn("1 из 2", progress)
        self.assertIn("Уже проголосовали: Аня", progress)
        self.assertIn("Ждём: Боб", progress)
        self.assertNotIn("guilty", progress)

    async def test_judge_verdict_waits_for_the_jury_with_progress(self):
        self.game.phase = "verdict"
        jury = [Player(id=i, game_id=1, name=f"П{i}", active=True) for i in (1, 2, 3)]
        self.court.vote_summary = AsyncMock(return_value=(jury, {"1": "guilty"}, False, False))
        self.court.finish = AsyncMock()
        await self.court.verdict(self.game, "guilty")
        self.court.finish.assert_not_awaited()
        self.assertIn("1 из 3", self.bot.send_message.await_args.args[1])

    async def test_all_jury_voted_reminds_the_judge_once(self):
        self.game.phase = "verdict"
        self.game.deadline = utcnow() + timedelta(seconds=120)
        self.game.state = {"speaker": "judge"}
        self.court.vote_summary = AsyncMock(return_value=([], {"1": "guilty"}, True, False))
        self.court.by_code = AsyncMock(return_value=None)
        await self.court.tick(self.game)
        texts = [call.args[1] for call in self.bot.send_message.await_args_list]
        self.assertTrue(any("Слово судье" in t for t in texts))
        self.assertTrue(self.game.state["jury_notice"])
        self.bot.send_message.reset_mock()
        await self.court.tick(self.game)
        texts = [call.args[1] for call in self.bot.send_message.await_args_list]
        self.assertFalse(any("Слово судье" in t for t in texts))

    async def test_mention_is_plain_for_bots_and_escaped_for_people(self):
        human = Player(id=1, game_id=1, user_id=42, name="Аня <A>", active=True)
        bot = Player(id=2, game_id=1, name="Бот 1", active=True, is_bot=True)
        self.assertIn('href="tg://user?id=42"', self.court.mention(human, "Судья"))
        self.assertIn("Аня &lt;A&gt;", self.court.mention(human, "Судья"))
        self.assertEqual(self.court.mention(bot, "Судья"), "Судья")

    async def test_public_memory_keeps_plain_text_without_markup(self):
        await self.court.public(
            self.game,
            '<a href="tg://user?id=1">Аня</a>, слово ваше.',
            memory="Аня, слово ваше.",
            parse_mode="HTML",
        )
        saved = self.db.add.call_args.args[0]
        self.assertEqual(saved.content, "Аня, слово ваше.")
        self.assertEqual(self.bot.send_message.await_args.kwargs["parse_mode"], "HTML")

    async def test_send_falls_back_to_plain_text_when_html_is_rejected(self):
        self.bot.send_message = AsyncMock(
            side_effect=[
                TelegramBadRequest(method=None, message="can't parse entities"),
                SimpleNamespace(message_id=5),
            ]
        )
        await self.court.send(-100, "<b>текст</b>", parse_mode="HTML")
        plain = self.bot.send_message.await_args.args[1]
        self.assertEqual(plain, "текст")
        self.assertNotIn("parse_mode", self.bot.send_message.await_args.kwargs)

    async def test_turn_state_text_reports_speaker_and_phase(self):
        self.game.phase = "debate"
        self.speaking("defense")
        text = await self.court.turn_state_text(self.game)
        self.assertIn("Слово: Адвокат", text)
        self.assertIn("я всё", text)


if __name__ == "__main__":
    unittest.main()
