"""migrate chat body from client-side E2EE to server-side at-rest encryption

Revision ID: 0005_chat_body_at_rest
Revises: 0004_encrypt_pii
Create Date: 2026-07-28

This migration downgrades chat_messages.body from client-side E2EE (Layer A)
to server-side field-level encryption at rest (Layer B), consistent with
other PII/PHI fields.

DATA MIGRATION:
- Existing body values are Layer-A ciphertext (base64-encoded 12-byte IV + AES-GCM ciphertext+tag)
- Each is decrypted using the consultation's e2ee_key (SHA-256 derived as AES key)
- The resulting plaintext is re-encrypted using DATABASE_ENCRYPTION_KEY (encrypt_value)
- Per-row decryption failures are logged and replaced with a placeholder

SAFETY: This migration requires DATABASE_ENCRYPTION_KEY to be set (already required
by models.py). The e2ee_key is read directly from the consultations table.
"""
import base64
import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.dialects import postgresql

# Import encryption utilities for re-encryption.
# Requires DATABASE_ENCRYPTION_KEY env var.
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from encryption import encrypt_value
except Exception as e:
    print(f"Warning: Could not import encryption module: {e}")
    print("Schema changes will proceed only if no existing data is at risk (see below).")
    encrypt_value = None

revision: str = "0005_chat_body_at_rest"
down_revision: Union[str, None] = "0004_encrypt_pii"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def derive_aes_key_from_e2ee_key(e2ee_key: str) -> bytes:
    """Derive AES-256 key from e2ee_key using SHA-256.
    
    This matches the browser's window.crypto.subtle.digest('SHA-256', ...) behavior:
    - UTF-8 encode the e2ee_key string
    - SHA-256 hash it
    - Use the raw digest bytes directly as the AES key (not re-encoded)
    """
    return hashlib.sha256(e2ee_key.encode('utf-8')).digest()


def decrypt_layer_a_ciphertext(ciphertext_b64: str, e2ee_key: str) -> str:
    """Decrypt Layer-A ciphertext using the consultation's e2ee_key.
    
    Args:
        ciphertext_b64: Base64-encoded ciphertext (12-byte IV + AES-GCM ciphertext+tag)
        e2ee_key: The consultation's e2ee_key (plaintext string)
        
    Returns:
        Decrypted plaintext string
        
    Raises:
        Exception: If decryption fails
    """
    # Base64-decode the stored body
    combined = base64.b64decode(ciphertext_b64)
    
    # Split into IV (first 12 bytes) and ciphertext+tag (remainder)
    iv = combined[:12]
    ciphertext = combined[12:]
    
    # Derive the AES key from e2ee_key
    aes_key = derive_aes_key_from_e2ee_key(e2ee_key)
    
    # Decrypt using AES-GCM
    aesgcm = AESGCM(aes_key)
    plaintext = aesgcm.decrypt(iv, ciphertext, None)
    
    return plaintext.decode('utf-8')


def upgrade() -> None:
    connection = op.get_bind()
    
    # ------------------------------------------------------------------
    # Step 1: add new nullable column alongside the existing Layer-A
    # ciphertext column. Purely additive - always safe.
    # ------------------------------------------------------------------
    op.add_column('chat_messages', sa.Column('body_encrypted', postgresql.BYTEA(), nullable=True))
    
    # ------------------------------------------------------------------
    # Step 2: figure out whether there is any existing chat data
    # that would be destroyed if we proceed without encryption keys.
    # ------------------------------------------------------------------
    row_count = connection.execute(sa.text("SELECT COUNT(*) FROM chat_messages")).scalar()
    has_existing_data = row_count > 0
    encryption_available = encrypt_value is not None
    
    if has_existing_data and not encryption_available:
        raise RuntimeError(
            "Migration 0005_chat_body_at_rest found existing chat messages but "
            "DATABASE_ENCRYPTION_KEY is not set. Refusing to proceed: continuing "
            "from here would drop the Layer-A ciphertext columns without ever "
            "re-encrypting their contents. Set the DATABASE_ENCRYPTION_KEY environment "
            "variable in this migration's environment and rerun `alembic upgrade head`. "
            "No changes have been committed."
        )
    
    # ------------------------------------------------------------------
    # Step 3: migrate existing data from Layer-A to Layer-B encryption.
    # ------------------------------------------------------------------
    if has_existing_data:
        # Fetch all chat messages with their consultation's e2ee_key
        result = connection.execute(sa.text("""
            SELECT cm.id, cm.consultation_id, cm.body, c.e2ee_key
            FROM chat_messages cm
            JOIN consultations c ON cm.consultation_id = c.consultation_id
        """))
        
        success_count = 0
        failure_count = 0
        failure_ids = []
        
        for row in result:
            try:
                # Decrypt Layer-A ciphertext using consultation's e2ee_key
                plaintext = decrypt_layer_a_ciphertext(row.body, row.e2ee_key)
                
                # Re-encrypt using DATABASE_ENCRYPTION_KEY (Layer-B)
                encrypted_body = encrypt_value(plaintext)
                
                # Update the row with the new encrypted value
                connection.execute(
                    sa.text("UPDATE chat_messages SET body_encrypted = :enc WHERE id = :id"),
                    {'enc': encrypted_body, 'id': row.id}
                )
                
                success_count += 1
            except Exception as e:
                # Per-row failure: log and store a placeholder
                failure_count += 1
                failure_ids.append(row.id)
                print(f"Failed to migrate chat message id={row.id}: {e}")
                
                # Store a placeholder message re-encrypted via encrypt_value
                placeholder = "[message unrecoverable during migration]"
                try:
                    encrypted_placeholder = encrypt_value(placeholder)
                    connection.execute(
                        sa.text("UPDATE chat_messages SET body_encrypted = :enc WHERE id = :id"),
                        {'enc': encrypted_placeholder, 'id': row.id}
                    )
                except Exception as placeholder_error:
                    print(f"Failed to store placeholder for chat message id={row.id}: {placeholder_error}")
        
        print(f"Data migration completed: {success_count} rows decrypted successfully, "
              f"{failure_count} rows failed (stored as placeholder).")
        if failure_ids:
            print(f"Failed message IDs: {failure_ids}")
    else:
        print("No existing chat messages found - skipping data migration (nothing to migrate).")
    
    # ------------------------------------------------------------------
    # Step 4: enforce NOT NULL on the encrypted column.
    # Safe unconditionally here: either there was data and it's now encrypted
    # (or replaced with placeholder), or there was none.
    # ------------------------------------------------------------------
    op.alter_column('chat_messages', 'body_encrypted', nullable=False)
    
    # ------------------------------------------------------------------
    # Step 5: drop old Layer-A ciphertext column and rename encrypted column
    # into place.
    # ------------------------------------------------------------------
    op.drop_column('chat_messages', 'body')
    op.alter_column('chat_messages', 'body_encrypted', new_column_name='body')


def downgrade() -> None:
    # WARNING: this downgrade is DESTRUCTIVE, not reversible. It does not
    # re-encrypt data back to Layer-A ciphertext - it recreates an empty
    # String(3000) body column and drops the encrypted one. Any chat content
    # encrypted by this migration's upgrade() is permanently lost on downgrade.
    # Only run this against a database you're comfortable losing chat data from
    # (e.g. resetting a dev/demo environment), never against production as a
    # "rollback".
    print("WARNING: downgrading 0005_chat_body_at_rest permanently discards encrypted chat content. "
          "This is NOT a safe rollback for production data.")
    
    op.add_column('chat_messages', sa.Column('body_old', sa.String(3000), nullable=True))
    op.alter_column('chat_messages', 'body', new_column_name='body_encrypted')
    op.alter_column('chat_messages', 'body_old', new_column_name='body')
    op.drop_column('chat_messages', 'body_encrypted')
