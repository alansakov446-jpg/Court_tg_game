import os
from urllib.parse import parse_qsl, urlencode

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# libpq-only parameters that asyncpg.connect() does not accept (TypeError at connect).
# Neon's copy-paste URL contains channel_binding=require by default.
UNSUPPORTED_PARAMS = frozenset({"channel_binding"})


def database_url(raw=None):
    url = os.environ["DATABASE_URL"] if raw is None else raw
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    base, separator, query = url.partition("?")
    if not separator:
        return base
    params = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in UNSUPPORTED_PARAMS:
            continue
        # asyncpg uses ssl, not libpq's sslmode query parameter.
        params.append(("ssl" if key == "sslmode" else key, value))
    return base + ("?" + urlencode(params) if params else "")


def connect():
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
