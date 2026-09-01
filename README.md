# Secure Teleconsultation Workflow (LiveKit)

A secure, healthcare-compliant video conferencing workflow built with **LiveKit**, **FastAPI** (Python), **PostgreSQL**, and **React** (Vite). This repository provides a robust foundation for teleconsultation applications, emphasizing strict access control, auditability, encryption of PII/PHI at rest, and zero-trust principles.

## 🏗️ Architecture & Tech Stack

* **Frontend:** React, Vite, LiveKit Components
* **Backend:** Python, FastAPI, LiveKit Server SDK
* **Database:** PostgreSQL, SQLAlchemy (async), Alembic migrations
* **WebRTC Infrastructure:** LiveKit Server
* **Encryption:**
  * Media (audio/video) end-to-end encryption via LiveKit's native frame-level E2EE.
  * Chat message content encrypted **in transit** via a dedicated `e2ee.py` module, in addition to being encrypted at rest in PostgreSQL like other PII/PHI fields.
  * PII/PHI fields (participant names, audit log details, chat sender identity) encrypted **at rest** in PostgreSQL at the application layer (AES-256-GCM), transparent to the rest of the backend via SQLAlchemy `TypeDecorator`s (`encryption.py`).
* **Authorization:** Centralized in `auth.py`, which is the single place actor assertions (who's making a request, and what they're allowed to do) are resolved and checked, rather than each endpoint reimplementing its own role/identity logic.
* **Security Focus:** Short-lived JWTs, strict room isolation, comprehensive audit logging, field-level encryption for compliance tracking, server-to-server authentication for privileged room creation, and a least-privilege database role for the running app.

---

## 🚀 Prerequisites

Before you begin, ensure you have the following installed on your system:

* [Node.js](https://nodejs.org/) (v16 or higher)
* [Python 3.10+](https://www.python.org/)
* [PostgreSQL 15+](https://www.postgresql.org/download/)
* [LiveKit Server](https://docs.livekit.io/realtime/self-hosting/local/) (or the LiveKit CLI installed locally)
* [OpenSSL](https://www.openssl.org/) (or any tool that can generate random base64 bytes — used once to generate encryption keys, see below)

---

## 🛠️ Project Structure

```text
sample-livekit-workflow/
├── backend/
│   ├── alembic/          # Migration environment + versioned schema history
│   │   ├── versions/     # One file per schema change
│   │   └── env.py
│   ├── alembic.ini
│   ├── auth.py           # Centralized authorization (actor assertion / access control)
│   ├── database.py       # Async engine, session factory, get_db dependency
│   ├── models.py         # SQLAlchemy ORM models (source of truth for schema)
│   ├── crud.py           # DB access / repository layer
│   ├── e2ee.py           # End-to-end encryption for messages in transit
│   ├── encryption.py     # AES-256-GCM field encryption + HMAC blind-index utilities (at rest)
│   ├── main.py            # FastAPI app, token generation, webhook handling
│   └── requirements.txt
├── frontend/              # React/Vite application UI
├── livekit/                # LiveKit server configuration (.yaml)
├── start-dev.ps1            # Launches all services at once
└── README.md
```

---

## 🔐 Database Field-Level Encryption

Certain columns — participant/doctor/patient names, waiting room entries, audit event details, and chat sender identity — are encrypted at rest using AES-256-GCM, keyed by two secrets you generate once per environment. This is **required**, not optional: `backend/models.py` imports `encryption.py`, which validates and loads both keys at import time and raises immediately if either is missing or malformed — the app will not start, and `alembic upgrade head` will not run, without them.

### Generate the keys (once per environment)

```powershell
openssl rand -base64 32   # -> DATABASE_ENCRYPTION_KEY
openssl rand -base64 32   # -> DATABASE_BLIND_INDEX_KEY
```

Run this **twice** — each variable needs its own independent 32-byte key. Do not reuse one value for both; `DATABASE_ENCRYPTION_KEY` performs randomized AES-GCM encryption, while `DATABASE_BLIND_INDEX_KEY` performs deterministic HMAC hashing so a small set of encrypted fields (currently `participant_name`) can still be looked up by exact value — mixing the two purposes under one key weakens both.

### ⚠️ Before you set these anywhere real

* **There is no recovery path if either key is lost.** Losing `DATABASE_ENCRYPTION_KEY` permanently makes every encrypted column unreadable (names, audit details, chat sender info). Losing `DATABASE_BLIND_INDEX_KEY` breaks waiting-room name lookups against existing rows. Store both in a real secrets manager for anything beyond local dev — not just a local `.env` file that only one person has a copy of.
* **Every running instance of the app must use the exact same keys.** There is no key versioning in the current implementation — if instances disagree, data encrypted by one instance can't be decrypted by another.
* **Rotating either key later is not a rolling deploy.** Because there's no key versioning, rotation requires: stop writes → re-encrypt all existing rows with the new key (a migration) → deploy the new key everywhere at once → resume.

---

## 🗄️ Database Setup (PostgreSQL)

The schema is managed entirely through **Alembic migrations** — there is no manual `.sql` dump to import. This means the schema is scripted and reproducible: anyone can run the migration against an empty database and get identical tables, and every change is tracked as its own versioned file in `backend/alembic/versions/`.

### 1. Create the database

Connect to your local PostgreSQL server and create an empty database (no tables yet — those come from Alembic):

```powershell
psql -U postgres -h 127.0.0.1
```

```sql
CREATE DATABASE tachafy_teleconsult;
\q
```

### 2. Configure the connection strings and encryption keys

Add `DATABASE_URL`, `MIGRATIONS_DATABASE_URL`, `DATABASE_ENCRYPTION_KEY`, and `DATABASE_BLIND_INDEX_KEY` to `backend/.env` (see [Environment Variables](#-environment-variables) below). **Set these before running migrations** — `alembic/env.py` loads `.env` before importing `models.py`, and `models.py` fails to import at all without both encryption keys present and valid.

Note there are now **two separate connection strings**, not one — see [Database Roles & Least-Privilege Access](#-database-roles--least-privilege-access) below for why.

### 3. Run the migrations

From inside `backend/`, with the virtual environment activated and dependencies installed (`pip install -r requirements.txt`, which includes `alembic` and `cryptography`):

```powershell
alembic upgrade head
```

Migrations always run under `MIGRATIONS_DATABASE_URL` (the privileged role), never under the app's runtime `DATABASE_URL` — see below.

Tip: to sanity-check that `.env`, the encryption keys, and the migration chain are all wired correctly *without* touching your database, run `alembic upgrade head --sql` first — it prints the migration's SQL instead of executing it, but still runs `env.py` (and therefore the key-loading check) end to end.

This creates all five tables: `consultations`, `waiting_room_entries`, `audit_events`, `processed_webhook_events`, `chat_messages`, and (as of migration `0008_app_role_least_privilege`) the dedicated `tachafy_app` database role the running app connects as. On a fresh/empty database, PII columns are created encrypted from the start. If you're running this migration against a database that already has data from before encryption was added, the migration will refuse to proceed (raising a clear error) unless both encryption keys are set — it will not silently drop existing plaintext data.

### 4. Verify

```powershell
psql "postgresql://postgres:<password>@localhost:5432/tachafy_teleconsult" -c "\dt"
```

You should see the five tables listed above. Spot-checking `SELECT doctor_name FROM consultations;` should show unreadable bytea data, not plaintext — that's expected; decrypted values are only ever visible through the API, not directly in the database.

> **Note:** whenever `backend/models.py` changes, generate a new migration with `alembic revision --autogenerate -m "description"` and re-run `alembic upgrade head` — don't edit the database by hand.

---

## 🔑 Database Roles & Least-Privilege Access

The app and Alembic migrations connect as **two different PostgreSQL roles**, not one:

| Role | Used by | Privileges |
|---|---|---|
| `postgres` | Alembic migrations only (`MIGRATIONS_DATABASE_URL`) | Full/superuser — needed to create tables, alter schema, and manage grants |
| `tachafy_app` | The running app at runtime (`DATABASE_URL`) | Least-privilege, table-by-table (see below) — **not** a superuser |

`tachafy_app` is created and scoped by migration `0008_app_role_least_privilege`, based on an audit of every endpoint in `main.py`:

| Table | Grants |
|---|---|
| `consultations` | `SELECT, INSERT, UPDATE` |
| `waiting_room_entries` | `SELECT, INSERT, UPDATE` |
| `chat_messages` | `SELECT, INSERT` |
| `processed_webhook_events` | `SELECT, INSERT` |
| `audit_events` | `SELECT, INSERT` — **no `UPDATE`/`DELETE`, ever** |

The `audit_events` restriction is deliberate and load-bearing: combined with the hash-chain columns (`row_hash`/`prev_row_hash`, see below), it means the app itself has no path to alter or erase audit history — enforced by PostgreSQL, not just application code.

Future migrations that add new tables automatically grant `tachafy_app` `SELECT, INSERT, UPDATE, DELETE` on them via `ALTER DEFAULT PRIVILEGES` (so the app isn't silently locked out of new tables) — any table that should be more restricted than that default (like `audit_events`) needs an explicit narrower `REVOKE` added in that table's own migration.

### ⚠️ Why this matters, and a known caveat

Earlier revisions of this project ran both the app and Alembic under the same `postgres` connection string. Because PostgreSQL superusers bypass all `GRANT`/`REVOKE` checks, any table-level restriction (e.g. an earlier `REVOKE UPDATE, DELETE ON audit_events`) had no real effect while the app connected as `postgres` — it only looked correct in `\dp` output. The `tachafy_app` role and the `DATABASE_URL` / `MIGRATIONS_DATABASE_URL` split exist specifically to make that restriction real.

This still isn't a complete tamper-proofing story: anyone with access to the `postgres` role (or the underlying DB host) can still `GRANT` privileges back to `tachafy_app`, or bypass it entirely. The `tachafy_app` restriction stops the *app's own* write path — and bugs, injection, or a compromised app process — from ever touching audit history; it does not defend against a fully privileged database administrator. Closing that gap requires anchoring `audit_events` hash-chain checkpoints outside the database (tracked as a pre-production TODO in the `0007_audit_hash_chain` migration).

### Verifying the restriction is real

After running `alembic upgrade head` and setting `tachafy_app`'s password, confirm it manually before trusting it:

```sql
SET ROLE tachafy_app;
UPDATE audit_events SET event_type = 'tampered' WHERE id = (SELECT MIN(id) FROM audit_events);
-- should fail with a permissions error
RESET ROLE;
```

---

## 🚦 Running the Application Locally

To run the full workflow, you will need to open four separate terminal instances: PostgreSQL (usually already running as a background service), the WebRTC server, the backend API, and the frontend client.

### 1. Ensure PostgreSQL is running

On Windows, check via `services.msc` (look for `postgresql-x64-<version>`) — it typically starts automatically. On Linux:

```bash
sudo systemctl start postgresql
```

If you haven't already, complete the [Database Setup](#-database-setup-postgresql) steps above before continuing — including generating and setting the encryption keys, and setting up both connection strings and the `tachafy_app` role as described in [Database Roles & Least-Privilege Access](#-database-roles--least-privilege-access).

### 2. Start the LiveKit Server

In your next terminal, launch the local LiveKit server using your configuration file:

```powershell
livekit-server --config livekit/livekit.yaml
```

### 3. Start the Backend (FastAPI)

In your next terminal, navigate to the backend directory, activate your virtual environment, and start the API server. Ensure your `.env` file is properly configured with your LiveKit API Key/Secret, the server-to-server token, `DATABASE_URL` (pointed at `tachafy_app`), `MIGRATIONS_DATABASE_URL` (pointed at `postgres`), **and** both encryption keys — the app will fail to start without the latter.

```powershell
cd backend
# Activate your virtual environment (Windows)
.\venv\Scripts\Activate.ps1
# Install the Python dependencies
pip install -r requirements.txt
# Apply any pending database migrations (runs under MIGRATIONS_DATABASE_URL / postgres)
alembic upgrade head
# Start the server (runs under DATABASE_URL / tachafy_app)
uvicorn main:app --reload --port 8000 --env-file .env
```

### 4. Start the Frontend (Vite)

In your last terminal, navigate to the frontend directory and start the Vite development server:

```powershell
cd frontend
# Install dependencies if you haven't already
npm install
# Start the frontend app
npm run dev
```

### ⚡ One-Command Launch

A `start-dev.ps1` script is included in the repo root to launch all services at once, each in its own PowerShell window.

```powershell
.\start-dev.ps1
```

This assumes dependencies have already been installed at least once (`pip install -r requirements.txt` and `npm install`), that a `venv` exists in `backend/`, that PostgreSQL is running, that `backend/.env` has `DATABASE_URL`, `MIGRATIONS_DATABASE_URL`, and both encryption keys set, and that migrations have already been applied (`alembic upgrade head`).

---

## 🎥 Video Consultation Features

Beyond the baseline "join a room and talk" flow, the teleconsultation module implements the following consultation-management features:

### Waiting Room / Admission Control

* Patients and observers who join before the doctor is present are held in a waiting room (`waiting_room_entries` table) rather than entering the LiveKit room directly.
* If the doctor is already in the room and it isn't locked, non-doctor participants are auto-admitted; otherwise their request queues with status `waiting` until the doctor takes action.
* The doctor sees a live waiting-room panel listing each pending participant's name and role, with **Admit** / **Deny** actions and a badge indicating how many requests are pending.
* Admission is enforced server-side at the `/token` endpoint — a denied or not-yet-admitted participant cannot obtain a LiveKit join token, not just be hidden from the UI.

### Host Moderation Controls

Doctor-only, in-call moderation actions, kept separate from ending the whole consultation:

| Action | What it does |
|---|---|
| **Lock / Unlock room** | While locked, no new non-doctor participant can obtain a join token; participants already connected are unaffected. |
| **Live participants panel** | Real-time list of everyone currently in the LiveKit room, read directly from the LiveKit server API. |
| **Mute participant** | Server-side mute of a specific participant's published audio/video tracks. |
| **Remove participant** | Disconnects a specific participant from the room without ending the consultation for anyone else. |

Every moderation action is written to the audit trail (`consultation.locked`, `consultation.unlocked`, `participant.muted_by_host`, `participant.removed_by_host`).

### Persistent Chat with Full History

* In-call chat messages are persisted to the `chat_messages` table in addition to being delivered live over LiveKit's data channel.
* Participants who join after messages were already sent, or who reconnect mid-consultation, see the full chat history rather than only messages sent while they happened to be connected — closing a gap common to WebRTC chat implementations that treat chat as purely ephemeral.
* The chat UI merges persisted history with live incoming messages, de-duplicating by sender/body/timestamp so a participant doesn't see their own sent messages appear twice once history is refetched.
* The chat panel is collapsed by default and toggled open on demand; while collapsed, an unread-message badge tracks how many new messages have arrived since it was last opened.

---

## 🛡️ Security & Healthcare Compliance Features

This workflow is designed with healthcare and enterprise security requirements in mind:

* **Ephemeral Rooms:** Rooms are dynamically created for specific consultations and automatically destroyed when empty or expired.
* **Strict Role-Based Access Control (RBAC):** Access tokens are generated with specific LiveKit Video Grants, ensuring patients, doctors, and observers have strictly defined permissions (e.g., publish vs. subscribe-only).
* **Short-Lived Tokens:** JWTs are minted with a low Time-To-Live (TTL) to minimize the attack surface in case of token interception.
* **Server-to-Server Authentication for Room Creation:** `POST /api/consultations` requires a bearer token, compared in constant time (`hmac.compare_digest`) against `LIVEKIT_S2S_TOKEN`, so only the Tachafy backend — not any browser client — can create a room.
* **Media & Chat Encryption:** Audio/video is protected by LiveKit's native end-to-end frame encryption; chat message content is encrypted at rest in PostgreSQL at the application layer (AES-256-GCM), same as other PII/PHI fields.
* **Field-Level Encryption at Rest:** Participant/doctor/patient names, waiting room entries, audit event details, and chat sender identity are encrypted at the application layer (AES-256-GCM) before being written to PostgreSQL, protecting against database-level exposure (backup theft, unauthorized DB access, SQL injection dumps) even though the fields remain queryable/joinable where the application needs them to be.
* **Comprehensive, Durable, Tamper-Evident Audit Logging:** Every critical event (room creation, token issuance, waiting-room admission/denial, host moderation actions, room termination, LiveKit webhook events) is persisted to the `audit_events` table, chained via SHA-256 row hashes (`row_hash`/`prev_row_hash`) so any historical edit is detectable on verification — surviving backend restarts, unlike the earlier in-memory implementation.
* **Failed Webhook Verification Is Also Audited:** an invalid or missing LiveKit webhook signature is logged to `audit_events` (`event_type=webhook.verification_failed`, with source IP and a reason code — never the credential itself), not silently dropped, so attempted forgery is visible in the tamper-evident trail alongside legitimate events.
* **Rate Limiting on Public-Facing Endpoints:** `POST /api/webhooks` is limited to 30 requests/minute per source IP, bounding how fast an attacker can generate audit-log entries via repeated failed verification attempts.
* **Least-Privilege Database Role:** The app connects at runtime as `tachafy_app`, a non-superuser role with no `UPDATE`/`DELETE` rights on `audit_events` at all, enforced by PostgreSQL itself — see [Database Roles & Least-Privilege Access](#-database-roles--least-privilege-access).
* **Transactional Integrity:** Consultation creation is wrapped in a database transaction; if LiveKit room creation fails, the consultation record is rolled back rather than left in an inconsistent state, while the failure itself is still recorded in the audit trail.

---

## 🔒 Environment Variables

You must create a `.env` file in the `backend/` directory with the following variables:

```
# PostgreSQL connection used by the running app at runtime.
# Points at the least-privilege 'tachafy_app' role (see Database Roles section) — never 'postgres'.
DATABASE_URL=postgresql+asyncpg://tachafy_app:your_app_password@localhost:5432/tachafy_teleconsult

# PostgreSQL connection used ONLY by Alembic migrations.
# Points at a privileged role (e.g. 'postgres') able to create tables and manage grants.
MIGRATIONS_DATABASE_URL=postgresql+asyncpg://postgres:your_superuser_password@localhost:5432/tachafy_teleconsult

# LiveKit
LIVEKIT_API_URL=http://localhost:7880
LIVEKIT_API_KEY=your_dev_key
LIVEKIT_API_SECRET=your_dev_secret

# Server-to-server authentication for privileged room-creation calls
# (POST /api/consultations). Only the Tachafy backend should hold this
# value — never exposed to any browser client.
# Generate with: openssl rand -base64 32
LIVEKIT_S2S_TOKEN=your_generated_s2s_token

# Database field-level encryption (required — the app will not start without these)
# Generate each with: openssl rand -base64 32
# These two keys must NOT be the same value as each other.
DATABASE_ENCRYPTION_KEY=your_generated_32_byte_key_base64
DATABASE_BLIND_INDEX_KEY=your_generated_32_byte_key_base64

# E2EE key derivation for LiveKit media encryption
# Generate with: openssl rand -base64 32
# Must be DIFFERENT values from DATABASE_ENCRYPTION_KEY and DATABASE_BLIND_INDEX_KEY — do not reuse either.
E2EE_MASTER_SECRET_V1=your_generated_32_byte_key_base64
E2EE_CURRENT_KEY_VERSION=1
```

(Never commit your `.env` file to version control. Use `.env.example` to track required keys — with placeholder, non-functional values only, never real generated keys.)

### Setting the `tachafy_app` password

`tachafy_app`'s password is not stored in any migration file. When running migration `0008_app_role_least_privilege`, optionally set:

```powershell
$env:TACHAFY_APP_DB_PASSWORD = "a-real-generated-password"
```

before `alembic upgrade head`, and the migration will apply it via `ALTER ROLE`. If left unset, the migration creates/leaves the role without changing its password and prints a reminder — set it manually via `psql` (`ALTER ROLE tachafy_app WITH PASSWORD '...';`) before pointing `DATABASE_URL` at it.

### 🔄 E2EE Master Secret Rotation

The E2EE key derivation supports seamless secret rotation:

1. **Generate a new secret:**
   ```bash
   openssl rand -base64 32
   ```
2. **Add the new version** to your environment (alongside, not replacing, the old one):
   ```
   E2EE_MASTER_SECRET_V1=<original_secret>
   E2EE_MASTER_SECRET_V2=<new_secret>
   ```
3. **Deploy** the updated environment. At this point, existing consultations still use V1 and new ones still use V1.
4. **Bump the current version:**
   ```
   E2EE_CURRENT_KEY_VERSION=2
   ```
5. **Deploy again.** New consultations are now stamped with `e2ee_key_version=2` and derive their key from the V2 secret.
6. **Retire old versions:** Old secret versions must stay present for as long as any consultation with that version could still be actively in progress or reconnecting. Since consultations are short-lived (`CONSULTATION_TTL_MINUTES`, default 60 minutes), old versions can typically be removed from the environment soon after rotation.