"""Single worker: durable games, long-polling, advisory lock, health accounting.

A green run must mean the bot was actually polling. The worker counts completed
get_updates cycles, rewrites health.json after every cycle, and exits non-zero
when a window produced no polling at all. Exit codes:

    0  window completed with at least one get_updates cycle
    1  any failure (get_me, lost lock connection, zero cycles, ...)
    3  another worker already holds the advisory lock

The Telegram offset lives in the bot_state table (alembic revision 0002); the
.offset file and actions/cache are fallbacks only, so cache eviction can no
longer reset the bot to re-reading a day of stale updates.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramConflictError
from sqlalchemy import select, text

from ai.gemini import Gemini, cause
from db.models import Game
from db.session import connect
from game.handlers import router
from game.service import Court
from scripts.healthz import verdict

log = logging.getLogger("court")

# Built once per process: aiogram forbids re-attaching a router to a new dispatcher.
dp = Dispatcher()
dp.include_router(router)

LOCK_ID = 742190018
OFFSET_KEY = "telegram_offset"
STREAK_KEY = "silent_lag_streak"
EXIT_LOCK_BUSY = 3
ALLOWED_UPDATES = ["message", "callback_query"]


def utc_stamp():
    """ISO-8601 UTC timestamp with second precision (for health.json)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def health_path():
    return Path(os.getenv("HEALTH_FILE", "health.json"))


def offset_path():
    return Path(os.getenv("OFFSET_FILE", ".offset"))


def write_json_atomic(path, payload):
    """Rewrite health.json so readers never observe a half-written document."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def read_offset_file(path):
    """Fallback offset source; returns None when missing or unreadable."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def lag_of(updates, offset):
    """Distance between the newest pending update_id and our cursor (0 = idle)."""
    return max(0, updates[-1].update_id - offset) if updates else 0


async def state_get(sessions, key):
    async with sessions() as session:
        return await session.scalar(
            text("SELECT value FROM bot_state WHERE key = :key"), {"key": key}
        )


async def state_set(sessions, key, value):
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO bot_state (key, value, updated_at) "
                "VALUES (:key, :value, now()) "
                "ON CONFLICT (key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = now()"
            ),
            {"key": key, "value": str(value)},
        )


async def load_offset(sessions):
    """(offset, source). Database copy is truth; the .offset file is a fallback."""
    try:
        raw = await state_get(sessions, OFFSET_KEY)
        if raw is not None:
            return int(raw), "database"
    except Exception as exc:  # noqa: BLE001 — a missing table must not stop startup
        log.warning(
            "Database offset unavailable (%s); using file fallback",
            type(exc).__name__,
        )
    file_offset = read_offset_file(offset_path())
    if file_offset is not None:
        return file_offset, "file"
    return 0, "fresh"


async def save_offset(value, sessions):
    """Persist only after the update transaction commits; DB first, file as fallback."""
    await state_set(sessions, OFFSET_KEY, value)
    try:
        temporary = offset_path().with_suffix(".tmp")
        temporary.write_text(str(value), encoding="utf-8")
        temporary.replace(offset_path())
    except OSError as exc:
        log.warning("Offset file not written (%s)", type(exc).__name__)


async def update_silent_streak(sessions, health):
    """Guard: backlog at window start that was never processed, 3 windows in a row.

    A healthy poller drains the start-of-window backlog within its first cycles,
    so 'pending updates exist but nothing was handled' is a dead-transport smell.
    """
    backlog_idle = health["updates_handled"] == 0 and health["start_lag"] > 0
    try:
        previous = int((await state_get(sessions, STREAK_KEY)) or 0)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail the window
        previous = 0
    streak = previous + 1 if backlog_idle else 0
    try:
        await state_set(sessions, STREAK_KEY, streak)
    except Exception:  # noqa: BLE001
        log.warning("Silent-streak state unavailable")
    health["silent_lag_streak"] = streak
    if streak >= 3:
        log.error(
            "Bot looks dead: %d consecutive windows with pending updates unprocessed",
            streak,
        )


async def start_health_site(port):
    """Optional in-process GET /healthz for PaaS health checks (Render/Fly/Railway)."""
    from aiohttp import web

    async def healthz(_request):
        ok, reason = verdict()
        return web.json_response(
            {"healthy": ok, "reason": reason}, status=200 if ok else 503
        )

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def run(*, engine=None, sessions=None, bot=None, ai=None, duration=None):
    """One worker window. engine/sessions/bot/ai may be injected for tests."""
    for name in ("BOT_TOKEN", "DATABASE_URL", "GEMINI_API"):
        if not os.getenv(name):
            raise RuntimeError(f"Missing environment variable: {name}")
    own_resources = engine is None
    if own_resources:
        engine, sessions = connect()
    if bot is None:
        bot = Bot(os.environ["BOT_TOKEN"])
    if ai is None:
        ai = Gemini(os.environ["GEMINI_API"])
    if duration is None:
        duration = int(os.getenv("RUN_SECONDS", "0"))  # 0 = continuous service
    end = time.monotonic() + duration if duration else float("inf")

    # Only counters, timestamps and the public bot username ever go in here.
    health = {
        "bot": None,
        "lock": "pending",
        "poll_cycles": 0,
        "updates_seen": 0,
        "updates_handled": 0,
        "started_at": utc_stamp(),
        "stopped_at": None,
        "offset": None,
        "offset_source": None,
        "pending_lag": 0,
        "start_lag": 0,
        "silent_lag_streak": 0,
        "run_seconds_limit": duration,
        "status": "starting",
    }
    write_json_atomic(health_path(), health)

    health_runner = None
    health_port = int(os.getenv("HEALTHZ_PORT", "0"))
    if health_port > 0:
        try:
            health_runner = await start_health_site(health_port)
            log.info("Health endpoint listening on 0.0.0.0:%d/healthz", health_port)
        except OSError as exc:
            # A broken healthcheck endpoint must not kill the poller itself.
            log.error(
                "Health endpoint could not bind port=%d reason=%s",
                health_port,
                type(exc).__name__,
            )

    try:
        # Dedicated session-level lock. Use a DIRECT Neon URL, not transaction pooling.
        async with engine.connect() as lock:
            acquired = await lock.scalar(
                text(f"SELECT pg_try_advisory_lock({LOCK_ID})")
            )
            if not acquired:
                log.info("Another worker is active; exiting")
                health.update(lock="busy", status="lock_busy", stopped_at=utc_stamp())
                write_json_atomic(health_path(), health)
                raise SystemExit(EXIT_LOCK_BUSY)
            health["lock"] = "ok"
            username = (await bot.get_me()).username
            health["bot"] = f"@{username}"
            await bot.delete_webhook(drop_pending_updates=False)
            offset, offset_source = await load_offset(sessions)
            health["offset"] = offset
            health["offset_source"] = offset_source
            try:
                # Read-only probe (offset is not acknowledged): measures the
                # backlog that queued up before this window opened.
                probe = await bot.get_updates(
                    offset=offset, timeout=0, limit=20, allowed_updates=ALLOWED_UPDATES
                )
                health["start_lag"] = lag_of(probe, offset)
            except TelegramConflictError:
                raise RuntimeError(
                    "Another Telegram poller is using BOT_TOKEN"
                ) from None
            except TelegramAPIError as exc:
                log.warning(
                    "Telegram temporarily unavailable during backlog probe (%s)",
                    type(exc).__name__,
                )
            log.info(
                "Polling started as @%s (%s) offset=%d source=%s start_lag=%d",
                username,
                f"{duration}s limit" if duration else "continuous",
                offset,
                offset_source,
                health["start_lag"],
            )
            health["status"] = "ok"
            while time.monotonic() < end:
                # A lost lock connection must terminate the worker, not silently reconnect.
                if lock.invalidated:
                    raise RuntimeError(
                        "Database lock connection was lost; restart required"
                    )
                await lock.execute(text("SELECT 1"))
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=5,
                        limit=20,
                        allowed_updates=ALLOWED_UPDATES,
                    )
                    health["poll_cycles"] += 1
                    health["updates_seen"] += len(updates)
                    health["pending_lag"] = max(
                        health["pending_lag"], lag_of(updates, offset)
                    )
                    for update in updates:
                        async with sessions() as session, session.begin():
                            court = Court(session, bot, ai, username)
                            await dp.feed_update(bot, update, court=court)
                        health["updates_handled"] += 1
                        offset = update.update_id + 1
                        # Only after the update transaction has committed.
                        await save_offset(offset, sessions)
                    async with sessions() as session:
                        ids = list(
                            (
                                await session.scalars(
                                    select(Game.id)
                                    .where(Game.status.in_(["lobby", "running"]))
                                    .order_by(Game.id)
                                )
                            ).all()
                        )
                    for gid in ids:
                        try:
                            async with sessions() as session, session.begin():
                                game = await session.get(Game, gid)
                                await Court(session, bot, ai, username).tick(game)
                        except Exception as exc:  # noqa: BLE001 — isolate one broken game from other rooms
                            # Do not log provider URLs, tokens, SQL parameters or private game context.
                            # cause() adds only the technical AI summary when there is one.
                            log.error(
                                "Game tick failed game=%s reason=%s", gid, cause(exc)
                            )
                    health["offset"] = offset
                    write_json_atomic(health_path(), health)
                except TelegramConflictError:
                    raise RuntimeError(
                        "Another Telegram poller is using BOT_TOKEN"
                    ) from None
                except TelegramAPIError as exc:
                    log.warning(
                        "Telegram temporarily unavailable (%s)", type(exc).__name__
                    )
                    await asyncio.sleep(3)
            # Confirm the final processed update even if there are no further polls.
            # Best effort: its failure must not mask the zero-cycles verdict below.
            try:
                await bot.get_updates(
                    offset=offset, timeout=0, limit=1, allowed_updates=ALLOWED_UPDATES
                )
            except TelegramConflictError:
                raise RuntimeError(
                    "Another Telegram poller is using BOT_TOKEN"
                ) from None
            except TelegramAPIError as exc:
                log.warning(
                    "Telegram unavailable during final confirm (%s)",
                    type(exc).__name__,
                )
            if health["poll_cycles"] == 0:
                # A window that never completed a poll is not a success.
                health["status"] = "no_polls"
                write_json_atomic(health_path(), health)
                raise RuntimeError(
                    "Window ended without a completed get_updates cycle"
                )
            await update_silent_streak(sessions, health)
            health["stopped_at"] = utc_stamp()
            write_json_atomic(health_path(), health)
            log.info(
                "Health polls=%d updates=%d handled=%d offset=%d lag=%d lock=%s",
                health["poll_cycles"],
                health["updates_seen"],
                health["updates_handled"],
                health["offset"],
                health["pending_lag"],
                health["lock"],
            )
            if duration:
                log.info("Polling stopped after configured %s-second limit", duration)
    finally:
        # Keep an explicit diagnosis (no_polls, lock_busy) instead of a generic error.
        if health["status"] not in ("ok", "lock_busy", "no_polls"):
            health["status"] = "error"
        if health["stopped_at"] is None:
            health["stopped_at"] = utc_stamp()
        try:
            write_json_atomic(health_path(), health)
        except OSError:
            log.warning("Final health.json could not be written")
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()
        await ai.close()
        if own_resources:
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    asyncio.run(run())
