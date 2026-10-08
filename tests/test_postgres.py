"""Run only against a dedicated disposable PostgreSQL database, never production."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Economy, Evidence, Game, Player, Role, utcnow
from game.rules import role_specs
from game.service import Court


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE economy, statements, evidence, players, roles, games RESTART IDENTITY CASCADE"
                )
            )
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.ai = SimpleNamespace(generate=AsyncMock())
        self.case = {
            "crime": "Исчезновение картины во время аукциона",
            "truth": "innocent",
            "evidence": [
                {
                    "title": f"След {i}",
                    "content": f"Факт {i}",
                    "interpretation": f"Скрытая трактовка {i}",
                    "visible_to_roles": ["public"] if i < 2 else ["witness_1"],
                }
                for i in range(6)
            ],
            "secrets": {r[0]: f"Личная информация {r[0]}" for r in role_specs(9)},
        }
        self.ai.generate.return_value = self.case

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def create_game(self, session):
        game = Game(
            chat_id=-100001,
            title="Интеграционный суд",
            creator_id=123,
            deadline=utcnow(),
            state={"requested_bots": 0},
        )
        session.add(game)
        await session.flush()
        return game

    async def test_start_persists_roles_wallets_and_filtered_memory(self):
        async with self.sessions() as session, session.begin():
            game = await self.create_game(session)
            session.add(Player(game_id=game.id, user_id=123, name="Игрок"))
            await session.flush()
            await Court(session, self.bot, self.ai, "test_bot").start(game)
            gid = game.id
        async with self.sessions() as session:
            game = await session.get(Game, gid)
            self.assertEqual(game.status, "running")
            self.assertEqual(game.phase, "crime")
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Player)), 9
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Role)), 9
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Evidence)), 6
            )
            self.assertEqual(
                list((await session.scalars(select(Economy.balance))).all()), [50, 50]
            )
            court = Court(session, self.bot, self.ai, "test_bot")
            role = await session.scalar(select(Role).where(Role.code == "defense"))
            context = await court.context(game, role)
            self.assertEqual(len(context["evidence"]), 2)
            self.assertIn("Личная информация defense", str(context))
            self.assertNotIn("Личная информация witness_1", str(context))
            self.assertNotIn("Скрытая трактовка", str(context))
            self.assertNotIn("truth", str(context))

    async def test_restart_keeps_permanent_replacement(self):
        async with self.sessions() as session, session.begin():
            game = await self.create_game(session)
            p = Player(game_id=game.id, user_id=123, name="Игрок")
            session.add(p)
            await session.flush()
            court = Court(session, self.bot, self.ai, "test_bot")
            await court.start(game)
            await court.leave(game, p)
            pid = p.id
        async with self.sessions() as session:
            p = await session.get(Player, pid)
            self.assertTrue(p.is_bot)
            self.assertTrue(p.permanent)

    async def test_exact_case_duplicate_never_starts(self):
        async with self.sessions() as session, session.begin():
            first = await self.create_game(session)
            court = Court(session, self.bot, self.ai, "test_bot")
            await court.start(first)
            first.status = "finished"
            await session.flush()
            second = await self.create_game(session)
            await court.start(second)
            self.assertEqual(second.status, "lobby")
            self.assertIsNone(second.case_hash)
            self.assertGreater(second.deadline, utcnow())

    async def test_jury_and_reward_survive_restart(self):
        async with self.sessions() as session, session.begin():
            game = await self.create_game(session)
            court = Court(session, self.bot, self.ai, "test_bot")
            await court.start(game)
            await court.set_phase(game, "verdict")
            jury = (
                await session.scalars(
                    select(Player)
                    .join(Role, Player.role_id == Role.id)
                    .where(Role.code.like("juror_%"))
                )
            ).all()
            from game.service import change

            change(game, votes={str(p.id): "innocent" for p in jury})
            await court.verdict(game, "guilty")
            gid = game.id
        async with self.sessions() as session, session.begin():
            game = await session.get(Game, gid)
            self.assertTrue(game.state["appealed"])
            await Court(session, self.bot, self.ai, "test_bot").verdict(
                game, "innocent"
            )
        async with self.sessions() as session:
            game = await session.get(Game, gid)
            self.assertEqual(game.status, "finished")
            balance = await session.scalar(
                select(Economy.balance).where(Economy.side == "defense")
            )
            self.assertEqual(balance, 150)

    async def test_join_exit_rejoin_and_old_link(self):
        from datetime import timedelta

        from game.handlers import handle

        def msg(content, *, private=False):
            return SimpleNamespace(
                text=content,
                chat=SimpleNamespace(
                    id=123 if private else -100001,
                    title="Тестовый чат",
                    type="private" if private else "supergroup",
                ),
                from_user=SimpleNamespace(id=123, full_name="Игрок", is_bot=False),
            )

        async with self.sessions() as session, session.begin():
            c = Court(session, self.bot, self.ai, "test_bot")
            await handle(msg("/game"), c)
            game = await c.active_game(-100001)
            self.assertIsNotNone(game)
            join = msg(f"/start join_{game.chat_id}_{game.id}", private=True)
            await handle(join, c)
            await session.flush()
            p = await c.player(game, 123)
            self.assertTrue(p.active)
            await handle(msg("/leave", private=True), c)
            self.assertFalse(p.active)
            await handle(join, c)
            self.assertTrue(p.active)
            await handle(msg("/extend 999"), c)
            self.assertEqual(game.deadline, game.created_at + timedelta(minutes=5))
            await handle(msg("/startnow"), c)
            await handle(msg("/leave", private=True), c)
            self.assertTrue(p.permanent)
            await handle(join, c)
            self.assertTrue(p.is_bot)
            self.assertTrue(p.permanent)

    async def test_full_all_bot_hearing(self):
        from datetime import timedelta

        async with self.sessions() as session, session.begin():
            game = await self.create_game(session)
            c = Court(session, self.bot, self.ai, "test_bot")
            await c.start(game)
            self.ai.generate.return_value = "Только факты из материалов дела."
            await c.bot_turn(game, await c.by_code(game, "judge"))
            for phase in ("accusation", "defense", "debate", "final", "verdict"):
                game.deadline = utcnow() - timedelta(seconds=1)
                await c.tick(game)
                self.assertEqual(game.phase, phase)
            self.ai.generate.return_value = {"vote": "innocent"}
            for _ in range(5):
                await c.tick(game)
            self.assertEqual(game.status, "finished")
            self.assertEqual(game.state["result"], "innocent")
