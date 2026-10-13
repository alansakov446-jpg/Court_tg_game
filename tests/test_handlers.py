import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from db.models import Game, Player, Role, utcnow
from game.handlers import (
    GROUP_COMMANDS,
    PRIVATE_COMMANDS,
    callback,
    handle,
    install_commands,
)
from game.rules import commands_need_mention, route_command


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="accusation",
            deadline=utcnow(),
            state={"speaker": "prosecutor"},
        )
        self.player = Player(
            id=2,
            game_id=1,
            user_id=10,
            name="Игрок",
            active=True,
            is_bot=False,
            permanent=False,
        )
        self.role = Role(
            id=3,
            game_id=1,
            code="prosecutor",
            title="Прокурор",
            side="prosecution",
        )
        self.court = SimpleNamespace(
            username="court_bot",
            active_game=AsyncMock(return_value=self.game),
            player=AsyncMock(return_value=self.player),
            role=AsyncMock(return_value=self.role),
            ask_host=AsyncMock(),
            send=AsyncMock(),
            public=AsyncMock(),
            record=AsyncMock(),
            give_floor=AsyncMock(),
            cast_vote=AsyncMock(return_value="Голос записан: виновен."),
            hand_over=AsyncMock(),
            refresh_lobby=AsyncMock(),
            lobby_card=AsyncMock(return_value="карточка зала"),
            vote_progress=AsyncMock(return_value="🗳 Голоса присяжных: 1 из 3."),
            turn_state_text=AsyncMock(return_value="Слово: Прокурор. Осталось 2:00."),
            db=SimpleNamespace(get=AsyncMock(return_value=None)),
        )

    @staticmethod
    def message(text, chat_type="supergroup"):
        """A group message without the bot's name in it (see group())."""
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100 if chat_type != "private" else 10,
                                type=chat_type, title="Тестовый суд"),
            from_user=SimpleNamespace(id=10, full_name="Игрок", is_bot=False),
        )

    @staticmethod
    def group(command):
        """How a command looks once it is addressed to this bot."""
        return f"{command}@court_bot" 

    async def test_ask_routes_a_free_form_question_to_ai_host(self):
        message = self.message("/ask@court_bot Где совершено преступление и чем?")
        await handle(message, self.court)
        self.court.ask_host.assert_awaited_once_with(
            self.game,
            self.player,
            "Где совершено преступление и чем?",
            -100,
            public=True,
        )

    async def test_empty_ask_is_still_validated_by_host_service(self):
        message = self.message("/ask@court_bot")
        await handle(message, self.court)
        self.court.ask_host.assert_awaited_once_with(
            self.game, self.player, "", -100, public=True
        )

    async def test_legacy_ask_id_still_interrogates_a_witness(self):
        witness = Player(id=4, game_id=1, name="Свидетель", active=True, is_bot=False)
        witness_role = Role(
            id=5, game_id=1, code="witness_1", title="Свидетель 1", side=None
        )
        self.game.state = {"speaker": "witness_1"}
        self.court.db.get.return_value = witness

        async def role_for(player):
            return witness_role if player is witness else self.role

        self.court.role.side_effect = role_for
        await handle(self.message("/ask@court_bot 4 Что вы видели?"), self.court)

        self.court.ask_host.assert_not_awaited()
        self.court.record.assert_awaited_once()
        self.assertIn("Что вы видели?", self.court.record.await_args.args[1])
        # The witness gets the floor at once, with the ping and the button.
        kwargs = self.court.give_floor.await_args.kwargs
        self.assertEqual(self.court.give_floor.await_args.args[1], "witness_1")
        self.assertEqual(kwargs["delay"], 3)
        self.assertEqual(kwargs["questioner"], "prosecutor")
        self.assertFalse(kwargs["handover"])
        self.assertIn("Что вы видели?", kwargs["note"])



class HandoverCommandTests(unittest.IsolatedAsyncioTestCase):
    """The wording of the commands and the spoken "я всё" forms."""

    def setUp(self):
        self.game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="accusation",
            deadline=utcnow(),
            state={"speaker": "prosecutor"},
        )
        self.player = Player(
            id=2, game_id=1, user_id=10, name="Игрок", active=True, violations=0
        )
        self.role = Role(
            id=3, game_id=1, code="prosecutor", title="Прокурор", side="prosecution"
        )
        self.court = SimpleNamespace(
            username="court_bot",
            active_game=AsyncMock(return_value=self.game),
            player=AsyncMock(return_value=self.player),
            role=AsyncMock(return_value=self.role),
            hand_over=AsyncMock(),
            send=AsyncMock(),
            public=AsyncMock(),
            record=AsyncMock(),
            fine=AsyncMock(),
            remove=AsyncMock(),
            refresh_lobby=AsyncMock(),
            lobby_card=AsyncMock(return_value="карточка зала"),
            vote_progress=AsyncMock(return_value="🗳 Голоса присяжных: 1 из 3."),
            turn_state_text=AsyncMock(return_value="Слово: Прокурор. Осталось 2:00."),
            db=SimpleNamespace(
                get=AsyncMock(return_value=None),
                scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
                add=MagicMock(),
            ),
        )

    @staticmethod
    def message(text, chat_type="supergroup"):
        """A group message without the bot's name in it (see group())."""
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100 if chat_type != "private" else 10,
                                type=chat_type, title="Тестовый суд"),
            from_user=SimpleNamespace(id=10, full_name="Игрок", is_bot=False),
        )

    @staticmethod
    def group(command):
        """How a command looks once it is addressed to this bot."""
        return f"{command}@court_bot" 

    async def test_pass_and_done_are_aliases_of_the_handover(self):
        for command in ("/pass@court_bot", "/done@court_bot", "/pass@Court_Bot"):
            self.court.hand_over.reset_mock()
            await handle(self.message(command), self.court)
            self.court.hand_over.assert_awaited_once_with(self.game, self.player)

    async def test_spoken_phrases_from_the_speaker_hand_the_word_over(self):
        for text in ("я всё", "На этом у меня всё", "всё сказал", "передаю слово"):
            self.court.hand_over.reset_mock()
            await handle(self.message(text), self.court)
            self.assertTrue(
                self.court.hand_over.await_count == 1, text
            )

    async def test_phrases_do_not_trigger_a_handover_for_other_players(self):
        self.role.code = "defense"  # not the speaker, and no floor in this phase
        await handle(self.message("я всё"), self.court)
        self.court.hand_over.assert_not_awaited()
        # Out-of-turn talk is still fined and counted, exactly as before.
        self.court.fine.assert_awaited_once()
        self.assertEqual(self.player.violations, 1)

    async def test_ordinary_speech_is_still_recorded_without_a_handover(self):
        await handle(self.message("Свидетель видел нож у склада."), self.court)
        self.court.hand_over.assert_not_awaited()
        self.court.record.assert_awaited_once()
        self.assertIn("нож", self.court.record.await_args.args[1])

    async def test_handover_refusal_is_explained_to_the_player(self):
        self.court.hand_over = AsyncMock(side_effect=ValueError("Сейчас говорит другая роль"))
        await handle(self.message("я всё"), self.court)
        self.assertIn("другая роль", self.court.send.await_args.args[1])

    async def test_state_shows_the_speaking_role(self):
        await handle(self.message("/state@court_bot"), self.court)
        self.court.send.assert_awaited_once_with(-100, "Слово: Прокурор. Осталось 2:00.")

    async def test_votes_command_reports_progress_in_the_voting_phase(self):
        self.game.phase = "verdict"
        await handle(self.message("/votes@court_bot"), self.court)
        self.assertIn("1 из 3", self.court.send.await_args.args[1])

    async def test_lobby_command_works_before_the_start(self):
        self.game.status = "lobby"
        await handle(self.message("/lobby@court_bot"), self.court)
        self.court.refresh_lobby.assert_awaited_once_with(self.game)
        self.assertIn("карточка зала", self.court.send.await_args.args[1])

    async def test_players_in_the_lobby_lists_the_hall(self):
        self.game.status = "lobby"
        await handle(self.message("/players@court_bot"), self.court)
        self.assertIn("карточка зала", self.court.send.await_args.args[1])

    async def test_say_without_a_text_is_reported(self):
        self.role.code = "juror_1"
        message = self.message("/say anon", "private")
        with (
            patch("game.handlers.locate", AsyncMock(return_value=self.game)),
            self.assertRaisesRegex(ValueError, "/say anon|public"),
        ):
            await handle(message, self.court)


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_menu_entry_is_a_valid_telegram_command(self):
        names = [name for name, _ in GROUP_COMMANDS + PRIVATE_COMMANDS]
        for name in names:
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
            self.assertNotIn(" ", name)
        for _name, description in GROUP_COMMANDS + PRIVATE_COMMANDS:
            self.assertTrue(3 <= len(description) <= 256, description)
        for command in ("pass", "done", "lobby", "state", "votes", "game", "help"):
            self.assertIn(command, names)

    async def test_install_commands_publishes_both_scopes(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        self.assertTrue(await install_commands(bot))
        self.assertEqual(bot.set_my_commands.await_count, 2)
        group = bot.set_my_commands.await_args_list[0]
        self.assertEqual(group.kwargs["scope"].type, "all_group_chats")
        self.assertIn("pass", [c.command for c in group.args[0]])

    async def test_a_rejected_menu_is_logged_and_never_breaks_the_poller(self):
        bot = SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=RuntimeError("bad description"))
        )
        with self.assertLogs("court.handlers", level="WARNING") as logs:
            self.assertFalse(await install_commands(bot))
        self.assertIn("Command menu not published", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()


class CommandAddressingTests(unittest.IsolatedAsyncioTestCase):
    """In a group the bot answers only to its own, properly addressed commands."""

    def setUp(self):
        self.game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="accusation",
            deadline=utcnow(),
            state={"speaker": "prosecutor"},
        )
        self.player = Player(
            id=2, game_id=1, user_id=10, name="Игрок", active=True, violations=0
        )
        self.role = Role(
            id=3, game_id=1, code="prosecutor", title="Прокурор", side="prosecution"
        )
        self.court = SimpleNamespace(
            username="court_bot",
            active_game=AsyncMock(return_value=self.game),
            player=AsyncMock(return_value=self.player),
            role=AsyncMock(return_value=self.role),
            hand_over=AsyncMock(),
            send=AsyncMock(),
            public=AsyncMock(),
            record=AsyncMock(),
            fine=AsyncMock(),
            ask_host=AsyncMock(),
            cmd=MagicMock(side_effect=lambda name, group=True: f"/{name}@court_bot"),
            db=SimpleNamespace(get=AsyncMock(return_value=None)),
        )

    @staticmethod
    def message(text, chat_type="supergroup"):
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(
                id=-100 if chat_type != "private" else 10,
                type=chat_type,
                title="Тестовый суд",
            ),
            from_user=SimpleNamespace(id=10, full_name="Игрок", is_bot=False),
        )

    async def test_bare_command_in_a_group_is_ignored_without_a_reply(self):
        for text in ("/pass", "/lobby", "/gg", "/start"):
            await handle(self.message(text), self.court)
        self.court.send.assert_not_awaited()
        self.court.hand_over.assert_not_awaited()
        self.court.ask_host.assert_not_awaited()

    async def test_addressed_command_works_and_unknown_one_is_answered(self):
        await handle(self.message("/pass@court_bot"), self.court)
        self.court.hand_over.assert_awaited_once_with(self.game, self.player)
        await handle(self.message("/gg@court_bot"), self.court)
        self.assertIn("Неизвестная команда", self.court.send.await_args.args[1])

    async def test_a_command_for_another_bot_is_ignored_even_when_unknown(self):
        for text in ("/gg@other_bot", "/pass@other_bot", "/start@other_bot"):
            await handle(self.message(text), self.court)
        self.court.send.assert_not_awaited()
        self.court.hand_over.assert_not_awaited()

    async def test_private_chat_needs_no_mention(self):
        with patch("game.handlers.locate", AsyncMock(return_value=self.game)):
            await handle(self.message("/pass", "private"), self.court)
        self.court.hand_over.assert_awaited_once_with(self.game, self.player)

    async def test_plain_speech_is_never_a_command(self):
        await handle(self.message("На этом у меня всё"), self.court)
        self.court.hand_over.assert_awaited_once_with(self.game, self.player)
        await handle(self.message("Свидетель видел нож."), self.court)
        self.court.record.assert_awaited()

    async def test_help_in_a_group_shows_the_addressed_form(self):
        await handle(self.message("/help@court_bot"), self.court)
        text = self.court.send.await_args.args[1]
        self.assertIn("/game@court_bot", text)
        await handle(self.message("/help", "private"), self.court)
        private = self.court.send.await_args.args[1]
        self.assertNotIn("@court_bot", private)


class CommandRoutingTests(unittest.TestCase):
    """The routing table itself, independent of aiogram and the database."""

    def test_plain_text_is_always_processed(self):
        for text in ("я всё", "Всем привет", "Протестую!", "-/pass", "1/2 дела"):
            self.assertEqual(route_command(text, "supergroup", "court_bot")[0], "text")

    def test_bare_group_command_is_ignored_when_mentions_are_required(self):
        self.assertEqual(
            route_command("/pass", "supergroup", "court_bot")[:2], ("ignore", "")
        )
        self.assertEqual(
            route_command("/game 2", "group", "court_bot", mention_required=True)[:2],
            ("ignore", ""),
        )

    def test_addressed_command_is_routed_with_its_argument(self):
        self.assertEqual(
            route_command(
                "/game@court_bot 2", "supergroup", "court_bot", mention_required=True
            ),
            ("command", "/game", "2"),
        )
        self.assertEqual(
            route_command(
                "/Pass@COURT_BOT", "supergroup", "court_bot", mention_required=True
            ),
            ("command", "/pass", ""),
        )

    def test_another_bots_command_is_ignored(self):
        self.assertEqual(
            route_command(
                "/gg@other_bot", "supergroup", "court_bot", mention_required=True
            )[:2],
            ("ignore", ""),
        )

    def test_private_chat_and_the_escape_hatch_accept_bare_commands(self):
        self.assertEqual(
            route_command("/pass", "private", "court_bot", mention_required=True)[0],
            "command",
        )
        self.assertEqual(
            route_command("/pass", "supergroup", "court_bot", mention_required=False)[0],
            "command",
        )

    def test_mention_requirement_comes_from_the_environment(self):
        self.assertTrue(commands_need_mention({}))
        self.assertTrue(commands_need_mention({"GROUP_COMMANDS_NEED_MENTION": "1"}))
        for value in ("0", "false", "no", "off", "OFF"):
            self.assertFalse(
                commands_need_mention({"GROUP_COMMANDS_NEED_MENTION": value}), value
            )


class StateCommandTests(unittest.IsolatedAsyncioTestCase):
    """Anyone in the chat may ask who is speaking, even without a role."""

    async def test_state_works_for_a_participant_and_for_a_stranger(self):
        game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="debate",
            deadline=utcnow(),
            state={"speaker": "defense"},
        )
        court = SimpleNamespace(
            username="court_bot",
            active_game=AsyncMock(return_value=game),
            player=AsyncMock(return_value=None),
            role=AsyncMock(return_value=None),
            send=AsyncMock(),
            turn_state_text=AsyncMock(return_value="Слово: Адвокат. Осталось 1:30."),
        )
        message = SimpleNamespace(
            text="/state@court_bot",
            chat=SimpleNamespace(id=-100, type="supergroup", title="Тестовый суд"),
            from_user=SimpleNamespace(id=99, full_name="Зритель", is_bot=False),
        )
        await handle(message, court)
        court.turn_state_text.assert_awaited_once_with(game)
        self.assertIn("Слово: Адвокат", court.send.await_args.args[1])


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    """Every button answers with a private toast, never with group noise."""

    def setUp(self):
        self.game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="verdict",
            deadline=utcnow(),
            state={"speaker": "judge"},
        )
        self.player = Player(id=2, game_id=1, user_id=10, name="Игрок", active=True)
        self.court = SimpleNamespace(
            username="court_bot",
            player=AsyncMock(return_value=self.player),
            hand_over=AsyncMock(),
            cast_vote=AsyncMock(return_value="Голос записан: виновен."),
            verdict=AsyncMock(),
            objection=AsyncMock(),
            leave=AsyncMock(),
            send=AsyncMock(),
            db=SimpleNamespace(get=AsyncMock(return_value=self.game)),
        )

    @staticmethod
    def query(data, answer=None):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=10, full_name="Игрок"),
            answer=answer or AsyncMock(),
        )

    async def test_pass_button_hands_the_word_over_with_a_toast(self):
        query = self.query("pass:1")
        await callback(query, self.court)
        self.court.hand_over.assert_awaited_once_with(self.game, self.player)
        query.answer.assert_awaited_once_with("Слово передано.", show_alert=False)

    async def test_vote_button_stores_the_choice_without_revealing_it(self):
        query = self.query("vote:1:guilty")
        await callback(query, self.court)
        self.court.cast_vote.assert_awaited_once_with(self.game, self.player, "guilty")
        query.answer.assert_awaited_once_with("Голос записан: виновен.", show_alert=False)

    async def test_judge_verdict_button_uses_the_same_service_call(self):
        query = self.query("verdict:1:innocent")
        await callback(query, self.court)
        self.court.verdict.assert_awaited_once_with(
            self.game, "innocent", by=self.player
        )
        query.answer.assert_awaited_once_with("Решение записано.", show_alert=False)

    async def test_refused_action_is_explained_in_a_toast_alert(self):
        self.court.cast_vote = AsyncMock(side_effect=ValueError("Голосует присяжный"))
        query = self.query("vote:1:guilty")
        await callback(query, self.court)
        query.answer.assert_awaited_once_with("Голосует присяжный", show_alert=True)

    async def test_an_expired_press_does_not_raise(self):
        from aiogram.exceptions import TelegramBadRequest

        query = self.query(
            "pass:1",
            answer=AsyncMock(
                side_effect=TelegramBadRequest(method=None, message="query is too old")
            ),
        )
        await callback(query, self.court)
        self.court.hand_over.assert_awaited_once()

    async def test_broken_button_data_is_ignored_gracefully(self):
        for data in ("", "pass", "pass:abc", "vote:1", None):
            query = self.query(data)
            await callback(query, self.court)
            self.assertTrue(query.answer.await_count >= 1, data)


class WitnessCommandTests(unittest.IsolatedAsyncioTestCase):
    """Calling a witness means an instant, addressed turn for that witness."""

    def setUp(self):
        self.game = Game(
            id=1,
            chat_id=-100,
            title="Тестовый суд",
            creator_id=10,
            status="running",
            phase="accusation",
            deadline=utcnow(),
            state={"speaker": "prosecutor"},
        )
        self.player = Player(id=2, game_id=1, user_id=10, name="Прокурор", active=True)
        self.witness = Player(id=7, game_id=1, user_id=77, name="Свидетель", active=True)
        self.role = Role(
            id=3, game_id=1, code="prosecutor", title="Прокурор", side="prosecution"
        )
        self.witness_role = Role(
            id=8, game_id=1, code="witness_1", title="Свидетель 1", side=None
        )
        self.court = SimpleNamespace(
            username="court_bot",
            active_game=AsyncMock(return_value=self.game),
            player=AsyncMock(return_value=self.player),
            role=AsyncMock(side_effect=self.role_for),
            give_floor=AsyncMock(),
            public=AsyncMock(),
            record=AsyncMock(),
            send=AsyncMock(),
            cast_vote=AsyncMock(return_value="Голос записан: виновен."),
            cmd=MagicMock(side_effect=lambda name, group=True: f"/{name}@court_bot"),
            db=SimpleNamespace(
                get=AsyncMock(return_value=self.witness),
                scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
            ),
        )

    async def role_for(self, player):
        return self.witness_role if player is self.witness else self.role

    def message(self, text):
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100, type="supergroup", title="Тестовый суд"),
            from_user=SimpleNamespace(id=10, full_name="Прокурор", is_bot=False),
        )

    async def test_call_gives_the_witness_the_floor_and_a_ping(self):
        await handle(self.message("/call@court_bot 7"), self.court)
        self.assertEqual(self.court.give_floor.await_args.args, (self.game, "witness_1"))
        kwargs = self.court.give_floor.await_args.kwargs
        self.assertEqual(kwargs["by"], self.role)
        self.assertEqual(kwargs["delay"], 3)
        self.assertEqual(kwargs["questioner"], "prosecutor")
        self.assertFalse(kwargs["handover"])
        self.assertIn("вызывает Свидетель 1", self.court.public.await_args.args[1])

    async def test_question_records_the_question_and_passes_it_to_the_witness(self):
        self.game.state["speaker"] = "witness_1"
        await handle(self.message("/question@court_bot 7 Что вы видели?"), self.court)
        self.assertIn("Что вы видели?", self.court.record.await_args.args[1])
        self.assertEqual(
            self.court.give_floor.await_args.kwargs["note"], "Вопрос: Что вы видели?"
        )

    async def test_question_without_a_text_still_asks_for_one(self):
        self.game.state["speaker"] = "witness_1"
        with self.assertRaisesRegex(ValueError, "ID вопрос"):
            await handle(self.message("/question@court_bot 7"), self.court)
        self.court.give_floor.assert_not_awaited()

    async def test_judge_words_keep_the_questioner_empty(self):
        self.role.code, self.role.title = "judge", "Судья"
        await handle(self.message("/word@court_bot 7"), self.court)
        self.assertEqual(self.court.give_floor.await_args.args, (self.game, "witness_1"))
        self.assertEqual(self.court.give_floor.await_args.kwargs["questioner"], "")

    async def test_vote_command_uses_the_shared_validator(self):
        self.game.phase = "verdict"
        self.role.code, self.role.title = "juror_1", "Присяжный 1"
        await handle(self.message("/vote@court_bot guilty"), self.court)
        self.assertEqual(self.court.send.await_args.args[1], self.court.cast_vote.return_value)

if __name__ == "__main__":
    unittest.main()
