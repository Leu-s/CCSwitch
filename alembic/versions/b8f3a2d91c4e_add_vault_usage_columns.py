"""Add vault usage snapshot columns to accounts

Revision ID: b8f3a2d91c4e
Revises: 3add7498b74c
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8f3a2d91c4e"
down_revision: Union[str, Sequence[str], None] = "3add7498b74c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("last_five_hour_resets_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_five_hour_utilization", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("last_seven_day_resets_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_seven_day_utilization", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("last_usage_probed_at", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("last_usage_probed_at")
        batch_op.drop_column("last_seven_day_utilization")
        batch_op.drop_column("last_seven_day_resets_at")
        batch_op.drop_column("last_five_hour_utilization")
        batch_op.drop_column("last_five_hour_resets_at")
