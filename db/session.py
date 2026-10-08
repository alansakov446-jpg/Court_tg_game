import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def database_url():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # asyncpg uses ssl, not libpq's sslmode query parameter.
    return url.replace("sslmode=", "ssl=")


def connect():
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
