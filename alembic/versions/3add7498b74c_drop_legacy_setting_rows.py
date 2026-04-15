"""drop_legacy_setting_rows

Revision ID: 3add7498b74c
Revises: a7e16baf4a03
Create Date: 2026-04-15 22:30:00.000000

Housekeeping: drop two Setting rows that the vault-swap rewrite no
longer reads but that older DBs still carry from before the cutover.

- ``original_credentials_backup``: JSON snapshot of ``~/.claude.json``
  that the pre-rewrite ``service/enable`` endpoint took so it could
  restore on disable.  The new endpoints do no such thing; the row is
  dead weight (often 50-100 KB of cached CLI state).
- ``credential_targets``: JSON map of canonical ``.claude.json`` paths
  the old mirror pipeline wrote to.  The mirror module and router are
  deleted; the row is unreferenced.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3add7498b74c"
down_revision: Union[str, Sequence[str], None] = "a7e16baf4a03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN "
        "('original_credentials_backup', 'credential_targets')"
    )


def downgrade() -> None:
    """No-op.  The dropped rows carried caller-specific state; rebuilding
    them from thin air would be meaningless, and the runtime code no
    longer reads either key."""
    pass
