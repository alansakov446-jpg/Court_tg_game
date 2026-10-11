import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db.models import Game, Player, Role, utcnow
from game.handlers import handle


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
            db=SimpleNamespace(get=AsyncMock(return_value=None)),
        )

    @staticmethod
    def message(text):
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100, type="supergroup", title="Тестовый суд"),
            from_user=SimpleNamespace(id=10, full_name="Игрок", is_bot=False),
        )

    async def test_ask_routes_a_free_form_question_to_ai_host(self):
        message = self.message("/ask Где совершено преступление и чем?")
        await handle(message, self.court)
        self.court.ask_host.assert_awaited_once_with(
            self.game,
            self.player,
            "Где совершено преступление и чем?",
            -100,
            public=True,
        )

    async def test_empty_ask_is_still_validated_by_host_service(self):
        message = self.message("/ask")
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
        await handle(self.message("/ask 4 Что вы видели?"), self.court)

        self.court.ask_host.assert_not_awaited()
        self.court.record.assert_awaited_once()
        self.assertIn("Что вы видели?", self.court.record.await_args.args[1])
        self.court.public.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
