"""drop_tmux_monitors

Revision ID: bcbe71a260df
Revises: faae4c20080a
Create Date: 2026-04-14 01:28:07.856429

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bcbe71a260df'
down_revision: Union[str, Sequence[str], None] = 'faae4c20080a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the tmux_monitors table.

    The user-managed monitor list was replaced with two settings rows:
    ``tmux_nudge_enabled`` and ``tmux_nudge_message``.  After every account
    switch, the background loop now scans every tmux pane and sends the
    nudge to any pane whose output looks like a rate-limit message — no
    user-defined patterns required.
    """
    with op.batch_alter_table('tmux_monitors', schema=None) as batch_op:
        batch_op.drop_index('ix_tmux_monitors_enabled')
    op.drop_table('tmux_monitors')


def downgrade() -> None:
    """Recreate the original tmux_monitors table."""
    op.create_table(
        'tmux_monitors',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('pattern_type', sa.String(16), nullable=False, server_default='manual'),
        sa.Column('pattern', sa.String(255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tmux_monitors_enabled', 'tmux_monitors', ['enabled'])
