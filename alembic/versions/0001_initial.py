"""Initial six game tables.

Revision ID: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "games",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False, index=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("creator_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_notice", sa.DateTime(timezone=True), nullable=False),
        sa.Column("case_hash", sa.String(64), unique=True),
        sa.Column("state", pg.JSONB, nullable=False),
    )
    op.create_index(
        "one_open_game_per_chat",
        "games",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('lobby', 'running')"),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False, index=True
        ),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("side", sa.String(20)),
        sa.UniqueConstraint("game_id", "code"),
    )
    op.create_table(
        "players",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False, index=True
        ),
        sa.Column("user_id", sa.BigInteger, index=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("role_id", sa.Integer, sa.ForeignKey("roles.id")),
        sa.Column("is_bot", sa.Boolean, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("permanent", sa.Boolean, nullable=False),
        sa.Column("removals", sa.Integer, nullable=False),
        sa.Column("violations", sa.Integer, nullable=False),
        sa.UniqueConstraint("game_id", "user_id"),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False, index=True
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("interpretation", sa.Text, nullable=False),
        sa.Column("visible_to_roles", pg.ARRAY(sa.Text), nullable=False),
    )
    op.create_table(
        "statements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False, index=True
        ),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id")),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("visible_to_roles", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "economy",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False, index=True
        ),
        sa.Column("side", sa.String(20), nullable=False),
        sa.Column("balance", sa.Integer, nullable=False),
        sa.UniqueConstraint("game_id", "side"),
    )


def downgrade():
    for name in ("economy", "statements", "evidence", "players", "roles", "games"):
        op.drop_table(name)
