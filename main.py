"""Single worker: durable games, bounded long-polling, advisory lock, cached offset."""

import asyncio
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramConflictError
from sqlalchemy import select, text

from ai.gemini import Gemini
from db.models import Game
from db.session import connect
from game.handlers import router
from game.service import Court

log = logging.getLogger("court")
OFFSET = Path(os.getenv("OFFSET_FILE", ".offset"))


def save_offset(value):
    temporary = OFFSET.with_suffix(".tmp")
    temporary.write_text(str(value), encoding="utf-8")
    temporary.replace(OFFSET)


async def run():
    for name in ("BOT_TOKEN", "DATABASE_URL", "GEMINI_API"):
        if not os.getenv(name):
            raise RuntimeError(f"Missing environment variable: {name}")
    engine, sessions = connect()
    bot = Bot(os.environ["BOT_TOKEN"])
    ai = Gemini(os.environ["GEMINI_API"])
    dp = Dispatcher()
    dp.include_router(router)
    duration = int(os.getenv("RUN_SECONDS", "0"))  # 0 = continuous service
    end = time.monotonic() + duration if duration else float("inf")
    try:
        # Dedicated session-level lock. Use a DIRECT Neon URL, not transaction pooling.
        async with engine.connect() as lock:
            acquired = await lock.scalar(text("SELECT pg_try_advisory_lock(742190018)"))
            if not acquired:
                log.info("Another worker is active; exiting")
                return
            username = (await bot.get_me()).username
            await bot.delete_webhook(drop_pending_updates=False)
            try:
                offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
            except ValueError:
                offset = 0
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
                        allowed_updates=["message", "callback_query"],
                    )
                    for update in updates:
                        async with sessions() as session, session.begin():
                            court = Court(session, bot, ai, username)
                            await dp.feed_update(bot, update, court=court)
                        offset = update.update_id + 1
                        save_offset(offset)  # Only after transaction commit.
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
                            log.error(
                                "Game tick failed (%s), game=%s",
                                type(exc).__name__,
                                gid,
                            )
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
            await bot.get_updates(
                offset=offset,
                timeout=0,
                limit=1,
                allowed_updates=["message", "callback_query"],
            )
    finally:
        await bot.session.close()
        await ai.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    asyncio.run(run())
