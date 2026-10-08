from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(Text)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="lobby", index=True)
    phase: Mapped[str] = mapped_column(String(20), default="lobby")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_notice: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    case_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        Index(
            "one_open_game_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("status IN ('lobby', 'running')"),
        ),
    )


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("game_id", "code"),)


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(Text)
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    permanent: Mapped[bool] = mapped_column(Boolean, default=False)
    removals: Mapped[int] = mapped_column(Integer, default=0)
    violations: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("game_id", "user_id"),)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    interpretation: Mapped[str] = mapped_column(Text)
    visible_to_roles: Mapped[list[str]] = mapped_column(ARRAY(Text))


class Statement(Base):
    __tablename__ = "statements"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    content: Mapped[str] = mapped_column(Text)
    visible_to_roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Economy(Base):
    __tablename__ = "economy"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    side: Mapped[str] = mapped_column(String(20))
    balance: Mapped[int] = mapped_column(Integer, default=50)
    __table_args__ = (UniqueConstraint("game_id", "side"),)
