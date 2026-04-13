"""initial_schema

Revision ID: b03dd0c6bdcf
Revises:
Create Date: 2026-04-13 23:59:21.028406

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b03dd0c6bdcf'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("config_dir", sa.String(512), nullable=False),
        sa.Column("threshold_pct", sa.Float(), nullable=False, server_default="95.0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_accounts_priority_enabled", "accounts", ["priority", "enabled"])
    op.create_index("ix_accounts_enabled", "accounts", ["enabled"])

    op.create_table(
        "tmux_monitors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("pattern_type", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("pattern", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tmux_monitors_enabled", "tmux_monitors", ["enabled"])

    op.create_table(
        "switch_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_account_id", sa.Integer(), nullable=True),
        sa.Column("to_account_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("triggered_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_switch_log_triggered_at", "switch_log", ["triggered_at"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_index("ix_switch_log_triggered_at", table_name="switch_log")
    op.drop_table("switch_log")
    op.drop_index("ix_tmux_monitors_enabled", table_name="tmux_monitors")
    op.drop_table("tmux_monitors")
    op.drop_index("ix_accounts_enabled", table_name="accounts")
    op.drop_index("ix_accounts_priority_enabled", table_name="accounts")
    op.drop_table("accounts")
