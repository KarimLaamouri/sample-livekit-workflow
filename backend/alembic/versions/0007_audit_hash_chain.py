"""append-only audit trail with hash chain and DB role hardening

Revision ID: 0007_audit_hash_chain
Revises: 0006_e2ee_key_derivation
Create Date: 2026-08-06

This migration adds:
- row_hash and prev_row_hash columns to audit_events for tamper-evidence
- DB role hardening: revoke UPDATE/DELETE, grant INSERT/SELECT on audit_events

The hash chain ensures any row tampering is detectable on verification.
Hash is computed over plaintext content (not ciphertext) to ensure verifiability.

DB ROLE: Runtime role is 'postgres' (from DATABASE_URL pattern in README).
Migrations run under a separate privileged role (same connection string currently).
"""
import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Import encryption utilities for decrypting audit details during backfill.
# Requires DATABASE_ENCRYPTION_KEY env var.
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from encryption import decrypt_json
except Exception as e:
    print(f"Warning: Could not import encryption module: {e}")
    print("Schema changes will proceed only if no existing data is at risk (see below).")
    decrypt_json = None

revision: str = "0007_audit_hash_chain"
down_revision: Union[str, None] = "0006_e2ee_key_derivation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # ------------------------------------------------------------------
    # Step 1: add new nullable columns
    # ------------------------------------------------------------------
    op.add_column('audit_events', sa.Column('row_hash', sa.String(64), nullable=True))
    op.add_column('audit_events', sa.Column('prev_row_hash', sa.String(64), nullable=True))

    # ------------------------------------------------------------------
    # Step 2: figure out whether there is any existing audit data
    # ------------------------------------------------------------------
    row_count = connection.execute(sa.text("SELECT COUNT(*) FROM audit_events")).scalar()
    has_existing_data = row_count > 0
    encryption_available = decrypt_json is not None

    if has_existing_data and not encryption_available:
        raise RuntimeError(
            "Migration 0007_audit_hash_chain found existing audit_events but "
            "DATABASE_ENCRYPTION_KEY is not set. Refusing to proceed: continuing "
            "from here would leave row_hash NULL for existing rows, breaking the "
            "chain. Set the DATABASE_ENCRYPTION_KEY environment variable in this "
            "migration's environment and rerun `alembic upgrade head`. "
            "No changes have been committed."
        )

    # ------------------------------------------------------------------
    # Step 3: backfill existing rows in id order so the chain is contiguous
    # from the very first row ever written.
    # ------------------------------------------------------------------
    if has_existing_data:
        result = connection.execute(
            sa.text("SELECT id, event_type, consultation_id, timestamp, details FROM audit_events ORDER BY id ASC")
        )
        prev_hash = None
        for row in result:
            # NOTE: `details` here is still ciphertext (BYTEA) at the raw-SQL
            # level — this migration runs outside the ORM, so EncryptedJSON's
            # automatic decryption doesn't apply. Decrypt explicitly using the
            # same DATABASE_ENCRYPTION_KEY the app uses.
            try:
                plaintext_details = decrypt_json(row.details) if row.details else None
            except Exception as e:
                print(f"WARNING: could not decrypt audit_events.id={row.id} details during backfill: {e}")
                plaintext_details = {"_backfill_decrypt_failed": True}

            payload = "|".join([
                row.event_type,
                row.consultation_id or "",
                row.timestamp.isoformat(),
                json.dumps(plaintext_details, sort_keys=True, default=str),
            ])
            row_hash = hashlib.sha256(f"{prev_hash or ''}{payload}".encode()).hexdigest()

            connection.execute(
                sa.text("UPDATE audit_events SET row_hash = :rh, prev_row_hash = :ph WHERE id = :id"),
                {'rh': row_hash, 'ph': prev_hash, 'id': row.id},
            )
            prev_hash = row_hash

        print(f"Data migration completed: {row_count} audit_events backfilled with hash chain.")
    else:
        print("No existing audit_events found - skipping data backfill (nothing to hash).")

    # ------------------------------------------------------------------
    # Step 4: enforce NOT NULL on row_hash
    # ------------------------------------------------------------------
    op.alter_column('audit_events', 'row_hash', nullable=False)

    # ------------------------------------------------------------------
    # Step 5: DB role hardening
    # Runtime role is 'postgres' (from DATABASE_URL pattern in README)
    # ------------------------------------------------------------------
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM postgres")
    op.execute("GRANT INSERT, SELECT ON audit_events TO postgres")


def downgrade() -> None:
    # Restore full privileges on audit_events
    op.execute("GRANT UPDATE, DELETE ON audit_events TO postgres")
    op.drop_column('audit_events', 'prev_row_hash')
    op.drop_column('audit_events', 'row_hash')
