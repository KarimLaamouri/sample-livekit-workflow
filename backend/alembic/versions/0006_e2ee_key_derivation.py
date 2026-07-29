"""replace stored e2ee_key with versioned server-derived key

Revision ID: 0006_e2ee_key_derivation
Revises: 0005_chat_body_at_rest
Create Date: 2026-07-29

This migration removes the plaintext e2ee_key column from the consultations
table and replaces it with an integer e2ee_key_version column.  Going forward
the actual E2EE key is derived at runtime via HMAC-SHA256(master_secret,
consultation_id) — no key material is persisted in the database.

DATA SAFETY:
- chat_messages.body was already migrated to DATABASE_ENCRYPTION_KEY in 0005.
- Media was never persisted.  There is NO old ciphertext requiring the
  original e2ee_key value to remain readable.
- Therefore the column can be dropped outright with no data-preservation step.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_e2ee_key_derivation"
down_revision: Union[str, None] = "0005_chat_body_at_rest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # ------------------------------------------------------------------
    # Step 1: Add e2ee_key_version column with server_default='1' so any
    # existing rows receive a valid version.
    # ------------------------------------------------------------------
    op.add_column(
        "consultations",
        sa.Column(
            "e2ee_key_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # ------------------------------------------------------------------
    # Step 2: Log how many existing rows are affected (safety/audit
    # pattern consistent with migrations 0004 and 0005).
    # ------------------------------------------------------------------
    row_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM consultations")
    ).scalar()

    if row_count > 0:
        print(
            f"INFO: {row_count} existing consultation(s) stamped with "
            f"e2ee_key_version=1 (server_default).  The plaintext e2ee_key "
            f"column will be dropped next — no downstream data depends on "
            f"the old value (chat_messages.body already migrated in 0005, "
            f"media was never persisted)."
        )
    else:
        print(
            "INFO: No existing consultations — adding e2ee_key_version "
            "and dropping e2ee_key on an empty table."
        )

    # ------------------------------------------------------------------
    # Step 3: Remove the server_default so future ORM inserts must
    # supply e2ee_key_version explicitly.
    # ------------------------------------------------------------------
    op.alter_column(
        "consultations",
        "e2ee_key_version",
        server_default=None,
    )

    # ------------------------------------------------------------------
    # Step 4: Drop the old plaintext e2ee_key column outright.
    # ------------------------------------------------------------------
    op.drop_column("consultations", "e2ee_key")


def downgrade() -> None:
    # WARNING: this downgrade CANNOT restore the original e2ee_key values.
    # The old column contained random tokens (secrets.token_urlsafe(32))
    # which have been replaced by a deterministic HMAC derivation with a
    # completely different formula.  There is no way to reverse-compute
    # the original random value from the new version integer.
    #
    # The recreated column is nullable and populated with NULL for all
    # rows.  This is acceptable because:
    #   - chat_messages.body no longer depends on e2ee_key (migrated in 0005)
    #   - media was never persisted
    #
    # If you need to roll back in production, new consultations created
    # after the downgrade will need application code that generates
    # e2ee_key again (i.e. reverting the Python changes alongside this
    # migration downgrade).
    print(
        "WARNING: downgrading 0006_e2ee_key_derivation — the original "
        "random e2ee_key values are permanently lost.  The column is "
        "recreated as nullable with NULL for all existing rows."
    )

    op.add_column(
        "consultations",
        sa.Column("e2ee_key", sa.String(length=64), nullable=True),
    )

    op.drop_column("consultations", "e2ee_key_version")
