"""create least-privilege runtime role (tachafy_app)

Revision ID: 0008_app_role_least_privilege
Revises: 0007_audit_hash_chain
Create Date: 2026-08-07

Context: migration 0007 revoked UPDATE/DELETE on audit_events FROM the
'postgres' role, but the app's runtime DATABASE_URL also connects as
'postgres', which is a PostgreSQL SUPERUSER (rolsuper = true). Superusers
bypass all GRANT/REVOKE checks entirely, so 0007's REVOKE has had no real
effect on the app's actual write path — it only ever restricted a role
nothing connects as at runtime.

This migration creates a genuinely non-superuser role, 'tachafy_app', and
grants it only what backend/main.py's endpoints actually use (verified by
reading every endpoint and crud.* call — no DELETE is issued anywhere in
that file against consultations, waiting_room_entries, chat_messages, or
processed_webhook_events; audit_events remains INSERT/SELECT-only).

NOTE on waiting_room_entries: models.py's Consultation.waiting_room_entries
relationship is declared with cascade="all, delete-orphan". No current code
path removes an entry from that Python-side collection directly (crud.py
only ever transitions status via set_waiting_room_status), so no DELETE is
issued today — but if future code ever does
`consultation.waiting_room_entries.remove(entry)` instead of going through
crud.py, SQLAlchemy will try to issue a DELETE and it will fail with a
permissions error under tachafy_app. That failure would be this migration
working as intended, not a bug — if that path is ever legitimately needed,
grant DELETE on waiting_room_entries explicitly in a follow-up migration
rather than reopening this one.

This migration does NOT change your app's DATABASE_URL. After running it:
  1. Set the role's password out of band (see step 2 below — never commit
     a real password to this file or to version control).
  2. Point backend/.env's DATABASE_URL at tachafy_app instead of postgres.
  3. Keep migrations running under 'postgres' (or a dedicated migration
     role) — a separate connection string, e.g. MIGRATIONS_DATABASE_URL.
  4. Verify manually in psql (SET ROLE tachafy_app; try an UPDATE on
     audit_events; confirm it's rejected) before trusting this as a real
     control.

CAUTION on password logging: if your Postgres instance has
log_statement = 'all' or 'mod' configured, the ALTER ROLE ... WITH
PASSWORD statement below can still land in plaintext in the server log
file itself, even though pg_stat_activity masks it. Check your instance's
log_statement setting before running this in any shared environment.

Grants are least-privilege per table, based on this migration's audit:
  - consultations           : SELECT, INSERT, UPDATE   (no DELETE — unused)
  - waiting_room_entries     : SELECT, INSERT, UPDATE   (no DELETE — unused; see NOTE above)
  - chat_messages            : SELECT, INSERT           (no UPDATE/DELETE — unused)
  - processed_webhook_events : SELECT, INSERT           (no UPDATE/DELETE — unused)
  - audit_events             : SELECT, INSERT           (no UPDATE/DELETE — by design)

ALTER DEFAULT PRIVILEGES ensures tables created by future migrations
(run as 'postgres') are automatically granted to tachafy_app too, with
audit_events-style tables excluded via an explicit narrower REVOKE
where needed — currently only audit_events itself needs that carve-out,
handled explicitly below since default privileges can't discriminate
by table name.

CAUTION: the default-privileges grant below gives tachafy_app full CRUD
(including DELETE) on any NEW table by default. Any future audit/log-style
table needs its own explicit REVOKE, the same way audit_events does here —
this default will not infer that for you. Re-audit grants explicitly
whenever a new table is added, don't rely on the default being correct.

If your endpoint set changes and a real delete path is added later
(e.g. a chat-purge feature), grant DELETE on that specific table in a
follow-up migration — don't reopen this one.
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_app_role_least_privilege"
down_revision: Union[str, None] = "0007_audit_hash_chain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Confirm this matches the actual database name in your connection string /
# settings before running — do not assume, verify (e.g. `\l` in psql or
# check whatever settings module builds DATABASE_URL).
DB_NAME = "tachafy_teleconsult"
APP_ROLE = "tachafy_app"

# Tables the app writes to, with the exact grants main.py's endpoints
# actually exercise (see audit in the module docstring above).
GRANTS = {
    "consultations": "SELECT, INSERT, UPDATE",
    "waiting_room_entries": "SELECT, INSERT, UPDATE",
    "chat_messages": "SELECT, INSERT",
    "processed_webhook_events": "SELECT, INSERT",
    "audit_events": "SELECT, INSERT",  # never UPDATE/DELETE — tamper-evidence depends on this
}


def upgrade() -> None:
    connection = op.get_bind()

    # ------------------------------------------------------------------
    # Step 1: create the role, idempotently, with NO password baked in.
    # Password is set separately (see step 2) from an env var so it never
    # lands in migration history / version control.
    # ------------------------------------------------------------------
    role_exists = connection.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": APP_ROLE},
    ).scalar()

    if not role_exists:
        op.execute(f"CREATE ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        print(f"Created role '{APP_ROLE}' (no password set yet).")
    else:
        print(f"Role '{APP_ROLE}' already exists — skipping CREATE ROLE.")

    # ------------------------------------------------------------------
    # Step 2: set/rotate the password from an env var, if provided.
    # This lets you run the same migration in every environment and have
    # each one pick up its own secret from its own env, rather than a
    # value hardcoded here. If unset, the role is left with whatever
    # password (or none) it already had — set it manually via psql if
    # you skip this.
    #
    # CAUTION: if this Postgres instance has log_statement = 'all' or
    # 'mod', this ALTER ROLE statement can be written to the server log
    # file in plaintext despite pg_stat_activity masking it. Check
    # log_statement before running this anywhere but an isolated local
    # dev instance.
    # ------------------------------------------------------------------
    app_role_password = os.environ.get("TACHAFY_APP_DB_PASSWORD")
    if app_role_password:
        # Never interpolate the password with plain string formatting into
        # SQL you'd log — op.execute here takes a literal, but avoid ever
        # printing app_role_password itself.
        escaped = app_role_password.replace("'", "''")
        op.execute(f"ALTER ROLE {APP_ROLE} WITH PASSWORD '{escaped}'")
        print(f"Password for '{APP_ROLE}' set from TACHAFY_APP_DB_PASSWORD.")
    else:
        print(
            f"TACHAFY_APP_DB_PASSWORD not set — leaving '{APP_ROLE}' password "
            f"unchanged. Set it manually before pointing DATABASE_URL at this "
            f"role: ALTER ROLE {APP_ROLE} WITH PASSWORD '...';"
        )

    # ------------------------------------------------------------------
    # Step 3: connection + schema access.
    # ------------------------------------------------------------------
    op.execute(f"GRANT CONNECT ON DATABASE {DB_NAME} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

    # ------------------------------------------------------------------
    # Step 4: per-table grants, scoped to what main.py's endpoints
    # actually do (see audit table above / module docstring).
    # ------------------------------------------------------------------
    for table, privileges in GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")

    # Sequences: needed for INSERTs into any SERIAL/identity column.
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # ------------------------------------------------------------------
    # Step 5: default privileges so future migrations (run as 'postgres')
    # don't silently leave tachafy_app without access to new tables.
    # This grants full CRUD by default; audit_events-style tables that
    # need a narrower grant must get an explicit REVOKE afterward, same
    # as the carve-out below for the existing audit_events table. See
    # the CAUTION in the module docstring — this default will not infer
    # a narrower grant for you on any future table.
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    # ------------------------------------------------------------------
    # Step 6: the actual point of this migration — no UPDATE/DELETE on
    # audit_events for the role the app will actually run as. This is
    # the fix for 0007's REVOKE having targeted a superuser and thus
    # done nothing in practice.
    # ------------------------------------------------------------------
    op.execute(f"REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE}")

    print(
        f"'{APP_ROLE}' created and scoped. Remaining manual steps: "
        f"(1) set its password if not done via TACHAFY_APP_DB_PASSWORD, "
        f"(2) update backend/.env DATABASE_URL to use it, "
        f"(3) keep a separate migrations-only connection string as 'postgres', "
        f"(4) verify with SET ROLE in psql before trusting this."
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {DB_NAME} FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")