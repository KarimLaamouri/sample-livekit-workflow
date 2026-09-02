"""add external_reference to consultations with partial unique index

Revision ID: 0013_consultation_external_reference
Revises: 0012_require_chat_ids
Create Date: 2026-09-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_consultation_external_reference"
down_revision: Union[str, None] = "0012_require_chat_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "consultations",
        sa.Column("external_reference", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_consultations_external_reference_active",
        "consultations",
        ["external_reference"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_consultations_external_reference_active", table_name="consultations"
    )
    op.drop_column("consultations", "external_reference")
