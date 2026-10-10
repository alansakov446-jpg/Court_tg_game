"""Health accounting, honest exit codes and the liveness probe.

A green run must mean the bot really polled: these tests pin the exit codes,
the health.json counters and the rule that no secret ever reaches health.json.
"""

import json
import os
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramAPIError

import main
from scripts.healthz import verdict

SECRET_TOKEN = "123456:TEST-TOKEN-do-not-leak"
SECRET_KEY = "AIzaTEST-KEY-do-not-leak"
SECRET_DB = "postgresql+asyncpg://user:hunter2@localhost/court"


class OffsetFileTests(unittest.TestCase):
    def test_valid_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".offset"
            path.write_text("42", encoding="utf-8")
            self.assertEqual(main.read_offset_file(path), 42)

    def test_missing_file_is_none(self):
        self.assertIsNone(main.read_offset_file(Path("/nonexistent/.offset")))

    def test_garbage_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".offset"
            path.write_text("not-a-number", encoding="utf-8")
            self.assertIsNone(main.read_offset_file(path))


class LagTests(unittest.TestCase):
    def test_empty_batch_is_no_lag(self):
        self.assertEqual(main.lag_of([], 42), 0)

    def test_distance_to_newest_pending_update(self):
        updates = [SimpleNamespace(update_id=43), SimpleNamespace(update_id=50)]
        self.assertEqual(main.lag_of(updates, 42), 8)

    def test_lag_is_never_negative(self):
        updates = [SimpleNamespace(update_id=10)]
        self.assertEqual(main.lag_of(updates, 100), 0)


class HealthFileTests(unittest.TestCase):
    def test_atomic_write_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            main.write_json_atomic(path, {"poll_cycles": 1, "lock": "ok"})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["poll_cycles"], 1)
            self.assertEqual(data["lock"], "ok")
            self.assertFalse(list(Path(tmp).glob("*.tmp")))


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "health.json"
        self.env = patch.dict(
            os.environ, {"HEALTH_FILE": str(self.path), "HEALTHZ_MAX_AGE_SECONDS": "120"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, **overrides):
        payload = {
            "bot": "@testbot",
            "lock": "ok",
            "poll_cycles": 5,
            "updates_handled": 2,
            "status": "ok",
        }
        payload.update(overrides)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_fresh_healthy_file_is_alive(self):
        self.write()
        ok, reason = verdict()
        self.assertTrue(ok)
        self.assertIn("polls=5", reason)

    def test_missing_file_is_dead(self):
        ok, reason = verdict()
        self.assertFalse(ok)
        self.assertIn("missing", reason)

    def test_stale_file_is_dead(self):
        self.write()
        old = time.time() - 500
        os.utime(self.path, (old, old))
        ok, reason = verdict()
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_busy_lock_is_dead(self):
        self.write(lock="busy", status="lock_busy", poll_cycles=0)
        ok, reason = verdict()
        self.assertFalse(ok)
        self.assertIn("lock=busy", reason)

    def test_error_status_is_dead(self):
        self.write(status="error")
        ok, reason = verdict()
        self.assertFalse(ok)
        self.assertIn("status=error", reason)

    def test_no_polls_window_is_dead(self):
        self.write(status="no_polls", poll_cycles=0)
        ok, reason = verdict()
        self.assertFalse(ok)
        self.assertIn("no_polls", reason)


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.invalidated = False

    async def scalar(self, statement, *args, **kwargs):
        return self.acquired

    async def execute(self, statement, *args, **kwargs):
        return None


class FakeEngine:
    def __init__(self, acquired=True):
        self.lock = FakeLock(acquired)

    def connect(self):
        lock = self.lock

        @asynccontextmanager
        async def cm():
            yield lock

        return cm()

    async def dispose(self):
        pass


class FakeSession:
    """Just enough surface for state_get/state_set and the game-id scan."""

    def __init__(self, state=None):
        self.state = state if state is not None else {}
        self.execute = AsyncMock(side_effect=self._execute)
        self.scalar = AsyncMock(side_effect=self._scalar)

    async def _scalar(self, statement, params=None, **kwargs):
        return self.state.get((params or {}).get("key"))

    async def _execute(self, statement, params=None, **kwargs):
        params = params or {}
        if "key" in params:
            self.state[params["key"]] = params.get("value")
        return None

    async def scalars(self, *args, **kwargs):
        return SimpleNamespace(all=lambda: [])

    def begin(self):
        @asynccontextmanager
        async def cm():
            yield

        return cm()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_bot():
    """get_me works, webhook dropped, every get_updates returns no updates."""
    return SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
        delete_webhook=AsyncMock(),
        get_updates=AsyncMock(return_value=[]),
        session=SimpleNamespace(close=AsyncMock()),
    )


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.health_file = Path(self.tmp.name) / "health.json"
        self.offset_file = Path(self.tmp.name) / ".offset"
        self.env = patch.dict(
            os.environ,
            {
                "BOT_TOKEN": SECRET_TOKEN,
                "DATABASE_URL": SECRET_DB,
                "GEMINI_API": SECRET_KEY,
                "OFFSET_FILE": str(self.offset_file),
                "HEALTH_FILE": str(self.health_file),
                "HEALTHZ_PORT": "0",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = {}

    def sessions(self):
        return FakeSession(self.state)

    def health(self):
        return json.loads(self.health_file.read_text(encoding="utf-8"))

    async def test_lock_busy_exits_with_code_3_and_reports_busy(self):
        with self.assertRaises(SystemExit) as cm:
            await main.run(
                engine=FakeEngine(acquired=False),
                sessions=self.sessions,
                bot=fake_bot(),
                ai=SimpleNamespace(close=AsyncMock()),
                duration=1,
            )
        self.assertEqual(cm.exception.code, main.EXIT_LOCK_BUSY)
        health = self.health()
        self.assertEqual(health["lock"], "busy")
        self.assertEqual(health["status"], "lock_busy")
        self.assertEqual(health["poll_cycles"], 0)

    async def test_normal_window_is_successful_and_reports_health(self):
        await main.run(
            engine=FakeEngine(),
            sessions=self.sessions,
            bot=fake_bot(),
            ai=SimpleNamespace(close=AsyncMock()),
            duration=1,
        )
        health = self.health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["lock"], "ok")
        self.assertEqual(health["bot"], "@testbot")
        self.assertGreaterEqual(health["poll_cycles"], 1)
        self.assertEqual(health["updates_seen"], 0)
        self.assertEqual(health["updates_handled"], 0)
        self.assertIsNotNone(health["started_at"])
        self.assertIsNotNone(health["stopped_at"])
        self.assertEqual(health["offset"], 0)
        self.assertEqual(health["offset_source"], "fresh")

    async def test_window_without_a_single_cycle_is_not_success(self):
        bot = fake_bot()
        bot.get_updates = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="unavailable")
        )
        with self.assertRaises(RuntimeError) as cm:
            await main.run(
                engine=FakeEngine(),
                sessions=self.sessions,
                bot=bot,
                ai=SimpleNamespace(close=AsyncMock()),
                duration=1,
            )
        self.assertIn("without a completed get_updates cycle", str(cm.exception))
        health = self.health()
        self.assertEqual(health["status"], "no_polls")
        self.assertEqual(health["poll_cycles"], 0)

    async def test_offset_roundtrip_and_file_fallback(self):
        self.state[main.OFFSET_KEY] = "12345"
        offset, source = await main.load_offset(self.sessions)
        self.assertEqual((offset, source), (12345, "database"))

        del self.state[main.OFFSET_KEY]
        self.offset_file.write_text("777", encoding="utf-8")
        offset, source = await main.load_offset(self.sessions)
        self.assertEqual((offset, source), (777, "file"))

        self.offset_file.unlink()
        offset, source = await main.load_offset(self.sessions)
        self.assertEqual((offset, source), (0, "fresh"))

    async def test_save_offset_writes_database_then_file(self):
        await main.save_offset(777, self.sessions)
        self.assertEqual(self.state[main.OFFSET_KEY], "777")
        self.assertEqual(self.offset_file.read_text(encoding="utf-8"), "777")

    async def test_silent_streak_counts_only_idle_backlog_windows(self):
        health = {"updates_handled": 0, "start_lag": 5, "silent_lag_streak": 0}
        await main.update_silent_streak(self.sessions, health)
        self.assertEqual(health["silent_lag_streak"], 1)
        await main.update_silent_streak(self.sessions, health)
        self.assertEqual(health["silent_lag_streak"], 2)

        # A window that handled updates resets the streak.
        health = {"updates_handled": 3, "start_lag": 5, "silent_lag_streak": 2}
        await main.update_silent_streak(self.sessions, health)
        self.assertEqual(health["silent_lag_streak"], 0)

    async def test_third_silent_backlog_window_logs_bot_looks_dead(self):
        self.state[main.STREAK_KEY] = "2"
        health = {"updates_handled": 0, "start_lag": 5, "silent_lag_streak": 0}
        with self.assertLogs("court", level="ERROR") as logs:
            await main.update_silent_streak(self.sessions, health)
        self.assertEqual(health["silent_lag_streak"], 3)
        self.assertIn("Bot looks dead", "\n".join(logs.output))

    async def test_health_file_never_contains_secrets(self):
        await main.run(
            engine=FakeEngine(),
            sessions=self.sessions,
            bot=fake_bot(),
            ai=SimpleNamespace(close=AsyncMock()),
            duration=1,
        )
        body = self.health_file.read_text(encoding="utf-8")
        for secret in (SECRET_TOKEN, SECRET_KEY, "hunter2", SECRET_DB):
            self.assertNotIn(secret, body)


if __name__ == "__main__":
    unittest.main()
