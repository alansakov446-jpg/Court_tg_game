import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

from ai.gemini import AIUnavailable
from db.models import Game, Player, Role, utcnow
from game.service import Court


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.bot = SimpleNamespace(send_message=AsyncMock())
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

    async def test_case_failure_is_logged_with_its_reason(self):
        self.game.status = "lobby"
        self.court.players = AsyncMock(return_value=[])
        empty = MagicMock(all=MagicMock(return_value=[]))
        self.db.scalars = AsyncMock(return_value=empty)
        failure = AIUnavailable("ИИ временно недоступен", summary="http=429 x10")
        with (
            patch("game.service.create_case", AsyncMock(side_effect=failure)),
            self.assertLogs("court.game", level="ERROR") as logs,
        ):
            await self.court.start(self.game)
        output = "\n".join(logs.output)
        self.assertIn("New case was not created game=1 players=0", output)
        self.assertIn("AIUnavailable: http=429 x10", output)
        self.assertIn("retry in 60s", output)
        self.assertEqual(self.game.status, "lobby")  # the room survives the failure

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
