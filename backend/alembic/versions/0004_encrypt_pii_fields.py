"""encrypt PII/PHI fields at rest

Revision ID: 0004_encrypt_pii
Revises: 0003_add_chat_messages
Create Date: 2026-07-27

This migration adds application-level encryption for PII/PHI columns:
- consultations.doctor_name, consultations.patient_name, consultations.ended_by
- waiting_room_entries.participant_name (with blind index for lookups)
- audit_events.details (JSONB -> encrypted JSON)
- chat_messages.sender_identity, chat_messages.sender_name

SAFETY: this migration checks for existing rows in every affected table before
doing anything destructive. If any table already has data AND
DATABASE_ENCRYPTION_KEY / DATABASE_BLIND_INDEX_KEY are not set, the migration
raises and aborts (rolled back by Alembic's transactional DDL) rather than
silently dropping plaintext PII columns with nothing encrypted to replace
them. On a genuinely empty database (fresh deploy), the migration proceeds
even without the keys set, since there is nothing to lose - though you should
still set both env vars before writing any real data, since new rows written
after this migration are encrypted at write time by the ORM layer, not by
this file.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Import encryption utilities for data migration.
# Requires DATABASE_ENCRYPTION_KEY and DATABASE_BLIND_INDEX_KEY env vars.
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from encryption import blind_index, encrypt_json, encrypt_value
except Exception as e:
    print(f"Warning: Could not import encryption module: {e}")
    print("Schema changes will proceed only if no existing data is at risk (see below).")
    encrypt_value = None
    encrypt_json = None
    blind_index = None

revision: str = "0004_encrypt_pii"
down_revision: Union[str, None] = "0003_add_chat_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # ------------------------------------------------------------------
    # Step 1: add new nullable columns alongside the existing plaintext
    # ones. Purely additive - always safe, regardless of key availability.
    # ------------------------------------------------------------------
    op.add_column('consultations', sa.Column('doctor_name_encrypted', postgresql.BYTEA(), nullable=True))
    op.add_column('consultations', sa.Column('patient_name_encrypted', postgresql.BYTEA(), nullable=True))
    op.add_column('consultations', sa.Column('ended_by_encrypted', postgresql.BYTEA(), nullable=True))

    op.add_column('waiting_room_entries', sa.Column('participant_name_encrypted', postgresql.BYTEA(), nullable=True))
    op.add_column('waiting_room_entries', sa.Column('participant_name_hash', sa.String(64), nullable=True))

    op.add_column('audit_events', sa.Column('details_encrypted', postgresql.BYTEA(), nullable=True))

    op.add_column('chat_messages', sa.Column('sender_identity_encrypted', postgresql.BYTEA(), nullable=True))
    op.add_column('chat_messages', sa.Column('sender_name_encrypted', postgresql.BYTEA(), nullable=True))

    # ------------------------------------------------------------------
    # Step 2: figure out whether there is any existing plaintext data
    # that would be destroyed if we proceed without encryption keys.
    # ------------------------------------------------------------------
    row_counts = {
        'consultations': connection.execute(sa.text("SELECT COUNT(*) FROM consultations")).scalar(),
        'waiting_room_entries': connection.execute(sa.text("SELECT COUNT(*) FROM waiting_room_entries")).scalar(),
        'audit_events': connection.execute(sa.text("SELECT COUNT(*) FROM audit_events")).scalar(),
        'chat_messages': connection.execute(sa.text("SELECT COUNT(*) FROM chat_messages")).scalar(),
    }
    has_existing_data = any(count > 0 for count in row_counts.values())
    encryption_available = encrypt_value is not None and encrypt_json is not None and blind_index is not None

    if has_existing_data and not encryption_available:
        at_risk_tables = [table for table, count in row_counts.items() if count > 0]
        raise RuntimeError(
            "Migration 0004_encrypt_pii found existing rows in "
            f"{at_risk_tables} but DATABASE_ENCRYPTION_KEY / DATABASE_BLIND_INDEX_KEY "
            "are not set. Refusing to proceed: continuing from here would drop the "
            "plaintext PII columns below without ever encrypting their contents. "
            "Set both environment variables in this migration's environment and "
            "rerun `alembic upgrade head`. No changes have been committed."
        )

    # ------------------------------------------------------------------
    # Step 3: encrypt existing data in place. Only runs when there is
    # data to migrate; encryption_available is guaranteed True here
    # whenever has_existing_data is True, per the check above.
    # ------------------------------------------------------------------
    if has_existing_data:
        result = connection.execute(
            sa.text("SELECT consultation_id, doctor_name, patient_name, ended_by FROM consultations")
        )
        for row in result:
            connection.execute(
                sa.text("""
                    UPDATE consultations
                    SET doctor_name_encrypted = :doc_enc,
                        patient_name_encrypted = :pat_enc,
                        ended_by_encrypted = :ended_enc
                    WHERE consultation_id = :id
                """),
                {
                    'doc_enc': encrypt_value(row.doctor_name) if row.doctor_name else None,
                    'pat_enc': encrypt_value(row.patient_name) if row.patient_name else None,
                    'ended_enc': encrypt_value(row.ended_by) if row.ended_by else None,
                    'id': row.consultation_id,
                },
            )

        result = connection.execute(sa.text("SELECT id, participant_name FROM waiting_room_entries"))
        for row in result:
            if row.participant_name:
                connection.execute(
                    sa.text("""
                        UPDATE waiting_room_entries
                        SET participant_name_encrypted = :enc,
                            participant_name_hash = :hash
                        WHERE id = :id
                    """),
                    {
                        'enc': encrypt_value(row.participant_name),
                        'hash': blind_index(row.participant_name),
                        'id': row.id,
                    },
                )

        result = connection.execute(sa.text("SELECT id, details FROM audit_events WHERE details IS NOT NULL"))
        for row in result:
            if row.details:
                connection.execute(
                    sa.text("UPDATE audit_events SET details_encrypted = :enc WHERE id = :id"),
                    {'enc': encrypt_json(row.details), 'id': row.id},
                )

        result = connection.execute(sa.text("SELECT id, sender_identity, sender_name FROM chat_messages"))
        for row in result:
            connection.execute(
                sa.text("""
                    UPDATE chat_messages
                    SET sender_identity_encrypted = :id_enc,
                        sender_name_encrypted = :name_enc
                    WHERE id = :id
                """),
                {
                    'id_enc': encrypt_value(row.sender_identity) if row.sender_identity else None,
                    'name_enc': encrypt_value(row.sender_name) if row.sender_name else None,
                    'id': row.id,
                },
            )

        print("Data migration completed: existing PII data encrypted.")
    else:
        print("No existing rows found in affected tables - skipping data backfill (nothing to encrypt).")

    # ------------------------------------------------------------------
    # Step 4: enforce NOT NULL on the encrypted columns (except
    # ended_by_encrypted, which is legitimately NULL for consultations
    # that haven't ended yet). Safe unconditionally here: either there
    # was data and it's now encrypted, or there was none, so the
    # constraint holds vacuously.
    # ------------------------------------------------------------------
    op.alter_column('consultations', 'doctor_name_encrypted', nullable=False)
    op.alter_column('consultations', 'patient_name_encrypted', nullable=False)
    op.alter_column('waiting_room_entries', 'participant_name_encrypted', nullable=False)
    op.alter_column('waiting_room_entries', 'participant_name_hash', nullable=False)
    op.alter_column('chat_messages', 'sender_identity_encrypted', nullable=False)
    op.alter_column('chat_messages', 'sender_name_encrypted', nullable=False)

    # ------------------------------------------------------------------
    # Step 5: drop old plaintext columns and rename encrypted columns
    # into place. Safe unconditionally now - the guard in Step 2 already
    # ensured no plaintext data reaches this point unencrypted.
    # ------------------------------------------------------------------
    op.drop_column('consultations', 'doctor_name')
    op.alter_column('consultations', 'doctor_name_encrypted', new_column_name='doctor_name')

    op.drop_column('consultations', 'patient_name')
    op.alter_column('consultations', 'patient_name_encrypted', new_column_name='patient_name')

    op.drop_column('consultations', 'ended_by')
    op.alter_column('consultations', 'ended_by_encrypted', new_column_name='ended_by')

    op.drop_constraint('uq_waiting_room_participant', 'waiting_room_entries', type_='unique')
    op.create_unique_constraint(
        'uq_waiting_room_participant', 'waiting_room_entries', ['consultation_id', 'participant_name_hash']
    )

    op.drop_column('waiting_room_entries', 'participant_name')
    op.alter_column('waiting_room_entries', 'participant_name_encrypted', new_column_name='participant_name')

    op.drop_column('audit_events', 'details')
    op.alter_column('audit_events', 'details_encrypted', new_column_name='details')

    op.drop_column('chat_messages', 'sender_identity')
    op.alter_column('chat_messages', 'sender_identity_encrypted', new_column_name='sender_identity')

    op.drop_column('chat_messages', 'sender_name')
    op.alter_column('chat_messages', 'sender_name_encrypted', new_column_name='sender_name')

    # Increase body column size for E2EE ciphertext (from Layer A)
    op.alter_column('chat_messages', 'body', type_=sa.String(3000))

    op.create_index(
        'ix_waiting_room_entries_participant_name_hash', 'waiting_room_entries', ['participant_name_hash']
    )


def downgrade() -> None:
    # WARNING: this downgrade is DESTRUCTIVE, not reversible. It does not
    # decrypt data back to plaintext - it recreates empty plaintext columns
    # and drops the encrypted ones. Any PII encrypted by this migration's
    # upgrade() is permanently lost on downgrade. Only run this against a
    # database you're comfortable losing PII data from (e.g. resetting a
    # dev/demo environment), never against production as a "rollback".
    print("WARNING: downgrading 0004_encrypt_pii permanently discards encrypted PII. "
          "This is NOT a safe rollback for production data.")

    op.drop_index('ix_waiting_room_entries_participant_name_hash', table_name='waiting_room_entries')

    op.add_column('consultations', sa.Column('doctor_name_old', sa.String(80), nullable=True))
    op.add_column('consultations', sa.Column('patient_name_old', sa.String(80), nullable=True))
    op.add_column('consultations', sa.Column('ended_by_old', sa.String(80), nullable=True))

    op.alter_column('consultations', 'doctor_name', new_column_name='doctor_name_encrypted')
    op.alter_column('consultations', 'doctor_name_old', new_column_name='doctor_name')

    op.alter_column('consultations', 'patient_name', new_column_name='patient_name_encrypted')
    op.alter_column('consultations', 'patient_name_old', new_column_name='patient_name')

    op.alter_column('consultations', 'ended_by', new_column_name='ended_by_encrypted')
    op.alter_column('consultations', 'ended_by_old', new_column_name='ended_by')

    op.drop_column('consultations', 'doctor_name_encrypted')
    op.drop_column('consultations', 'patient_name_encrypted')
    op.drop_column('consultations', 'ended_by_encrypted')

    op.drop_constraint('uq_waiting_room_participant', 'waiting_room_entries', type_='unique')
    op.create_unique_constraint(
        'uq_waiting_room_participant', 'waiting_room_entries', ['consultation_id', 'participant_name']
    )

    op.add_column('waiting_room_entries', sa.Column('participant_name_old', sa.String(80), nullable=True))
    op.alter_column('waiting_room_entries', 'participant_name', new_column_name='participant_name_encrypted')
    op.alter_column('waiting_room_entries', 'participant_name_old', new_column_name='participant_name')
    op.drop_column('waiting_room_entries', 'participant_name_encrypted')
    op.drop_column('waiting_room_entries', 'participant_name_hash')

    op.add_column('audit_events', sa.Column('details_old', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.alter_column('audit_events', 'details', new_column_name='details_encrypted')
    op.alter_column('audit_events', 'details_old', new_column_name='details')
    op.drop_column('audit_events', 'details_encrypted')

    op.add_column('chat_messages', sa.Column('sender_identity_old', sa.String(160), nullable=True))
    op.add_column('chat_messages', sa.Column('sender_name_old', sa.String(80), nullable=True))

    op.alter_column('chat_messages', 'sender_identity', new_column_name='sender_identity_encrypted')
    op.alter_column('chat_messages', 'sender_identity_old', new_column_name='sender_identity')

    op.alter_column('chat_messages', 'sender_name', new_column_name='sender_name_encrypted')
    op.alter_column('chat_messages', 'sender_name_old', new_column_name='sender_name')

    op.drop_column('chat_messages', 'sender_identity_encrypted')
    op.drop_column('chat_messages', 'sender_name_encrypted')

    op.alter_column('chat_messages', 'body', type_=sa.String(2000))