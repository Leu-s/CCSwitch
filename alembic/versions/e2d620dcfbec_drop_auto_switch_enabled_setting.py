"""drop_auto_switch_enabled_setting

Revision ID: e2d620dcfbec
Revises: bcbe71a260df
Create Date: 2026-04-14 03:33:56.821327

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2d620dcfbec'
down_revision: Union[str, Sequence[str], None] = 'bcbe71a260df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Delete the stale ``auto_switch_enabled`` Setting row.

    The key was consolidated into ``service_enabled`` (one master toggle)
    several commits ago, but no migration removed existing rows from live
    DBs, so ``GET /api/settings`` kept surfacing a ghost row that the code
    no longer reads.  This data-only migration drops it on next upgrade.
    """
    op.execute("DELETE FROM settings WHERE key = 'auto_switch_enabled'")


def downgrade() -> None:
    """No-op: the key is dead and would not be honoured if re-inserted."""
    pass
