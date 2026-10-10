"""Durable key/value state for the poller (Telegram offset, silent-streak counter).

The offset used to live in actions/cache, which can be evicted; an evicted
offset resets the bot to re-reading up to a day of stale updates.

Revision ID: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("bot_state")
