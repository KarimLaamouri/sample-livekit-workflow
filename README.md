# Secure Teleconsultation Workflow (LiveKit)

A secure, healthcare-compliant video conferencing workflow built with **LiveKit**, **FastAPI** (Python), **PostgreSQL**, and **React** (Vite). This repository provides a robust foundation for teleconsultation applications, emphasizing strict access control, auditability, encryption of PII/PHI at rest, and zero-trust principles.

## 🏗️ Architecture & Tech Stack

* **Frontend:** React, Vite, LiveKit Components
* **Backend:** Python, FastAPI, LiveKit Server SDK
* **Database:** PostgreSQL, SQLAlchemy (async), Alembic migrations
* **WebRTC Infrastructure:** LiveKit Server
* **Encryption:**
  * Media (audio/video) end-to-end encryption via LiveKit's native frame-level E2EE.
  * Chat message content encrypted client-side (AES-GCM, Web Crypto API) before it ever reaches the backend — the server persists ciphertext only.
  * PII/PHI fields (participant names, audit log details) encrypted at rest in PostgreSQL at the application layer (AES-256-GCM), transparent to the rest of the backend via SQLAlchemy `TypeDecorator`s.
* **Security Focus:** Short-lived JWTs, strict room isolation, comprehensive audit logging, and field-level encryption for compliance tracking.

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
│   ├── database.py       # Async engine, session factory, get_db dependency
│   ├── models.py         # SQLAlchemy ORM models (source of truth for schema)
│   ├── crud.py           # DB access / repository layer
│   ├── encryption.py     # AES-256-GCM field encryption + HMAC blind-index utilities
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

### 2. Configure the connection string and encryption keys

Add `DATABASE_URL`, `DATABASE_ENCRYPTION_KEY`, and `DATABASE_BLIND_INDEX_KEY` to `backend/.env` (see [Environment Variables](#-environment-variables) below). **Set these before running migrations** — `alembic/env.py` loads `.env` before importing `models.py`, and `models.py` fails to import at all without both encryption keys present and valid.

### 3. Run the migrations

From inside `backend/`, with the virtual environment activated and dependencies installed (`pip install -r requirements.txt`, which includes `alembic` and `cryptography`):

```powershell
alembic upgrade head
```

Tip: to sanity-check that `.env`, the encryption keys, and the migration chain are all wired correctly *without* touching your database, run `alembic upgrade head --sql` first — it prints the migration's SQL instead of executing it, but still runs `env.py` (and therefore the key-loading check) end to end.

This creates all five tables: `consultations`, `waiting_room_entries`, `audit_events`, `processed_webhook_events`, `chat_messages`. On a fresh/empty database, PII columns are created encrypted from the start. If you're running this migration against a database that already has data from before encryption was added, the migration will refuse to proceed (raising a clear error) unless both encryption keys are set — it will not silently drop existing plaintext data.

### 4. Verify

```powershell
psql "postgresql://postgres:<password>@localhost:5432/tachafy_teleconsult" -c "\dt"
```

You should see the five tables listed above. Spot-checking `SELECT doctor_name FROM consultations;` should show unreadable bytea data, not plaintext — that's expected; decrypted values are only ever visible through the API, not directly in the database.

> **Note:** whenever `backend/models.py` changes, generate a new migration with `alembic revision --autogenerate -m "description"` and re-run `alembic upgrade head` — don't edit the database by hand.

---

## 🚦 Running the Application Locally

To run the full workflow, you will need to open four separate terminal instances: PostgreSQL (usually already running as a background service), the WebRTC server, the backend API, and the frontend client.

### 1. Ensure PostgreSQL is running

On Windows, check via `services.msc` (look for `postgresql-x64-<version>`) — it typically starts automatically. On Linux:

```bash
sudo systemctl start postgresql
```

If you haven't already, complete the [Database Setup](#-database-setup-postgresql) steps above before continuing — including generating and setting the encryption keys.

### 2. Start the LiveKit Server

In your next terminal, launch the local LiveKit server using your configuration file:

```powershell
livekit-server --config livekit/livekit.yaml
```

### 3. Start the Backend (FastAPI)

In your next terminal, navigate to the backend directory, activate your virtual environment, and start the API server. Ensure your `.env` file is properly configured with your LiveKit API Key/Secret, `DATABASE_URL`, **and** both encryption keys — the app will fail to start without the latter.

```powershell
cd backend
# Activate your virtual environment (Windows)
.\venv\Scripts\Activate.ps1
# Install the Python dependencies
pip install -r requirements.txt
# Apply any pending database migrations
alembic upgrade head
# Start the server
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

This assumes dependencies have already been installed at least once (`pip install -r requirements.txt` and `npm install`), that a `venv` exists in `backend/`, that PostgreSQL is running, that `backend/.env` has `DATABASE_URL` and both encryption keys set, and that migrations have already been applied (`alembic upgrade head`).

---

## 🛡️ Security & Healthcare Compliance Features

This workflow is designed with healthcare and enterprise security requirements in mind:

* **Ephemeral Rooms:** Rooms are dynamically created for specific consultations and automatically destroyed when empty or expired.
* **Strict Role-Based Access Control (RBAC):** Access tokens are generated with specific LiveKit Video Grants, ensuring patients, doctors, and observers have strictly defined permissions (e.g., publish vs. subscribe-only).
* **Short-Lived Tokens:** JWTs are minted with a low Time-To-Live (TTL) to minimize the attack surface in case of token interception.
* **Media & Chat Encryption:** Audio/video is protected by LiveKit's native end-to-end frame encryption; chat message content is separately encrypted client-side before it reaches the backend, so the server never has access to plaintext chat content.
* **Field-Level Encryption at Rest:** Participant/doctor/patient names, waiting room entries, audit event details, and chat sender identity are encrypted at the application layer (AES-256-GCM) before being written to PostgreSQL, protecting against database-level exposure (backup theft, unauthorized DB access, SQL injection dumps) even though the fields remain queryable/joinable where the application needs them to be.
* **Comprehensive, Durable Audit Logging:** Every critical event (room creation, token issuance, waiting-room admission/denial, room termination, LiveKit webhook events) is persisted to the `audit_events` table in PostgreSQL — surviving backend restarts, unlike the earlier in-memory implementation.
* **Transactional Integrity:** Consultation creation is wrapped in a database transaction; if LiveKit room creation fails, the consultation record is rolled back rather than left in an inconsistent state, while the failure itself is still recorded in the audit trail.

---

## 🔒 Environment Variables

You must create a `.env` file in the `backend/` directory with the following variables:

```
# PostgreSQL connection (used by both the app and Alembic)
DATABASE_URL=postgresql+asyncpg://postgres:your_password@localhost:5432/tachafy_teleconsult

# LiveKit
LIVEKIT_API_URL=http://localhost:7880
LIVEKIT_API_KEY=your_dev_key
LIVEKIT_API_SECRET=your_dev_secret

# Database field-level encryption (required — the app will not start without these)
# Generate each with: openssl rand -base64 32
# These two keys must NOT be the same value as each other.
DATABASE_ENCRYPTION_KEY=your_generated_32_byte_key_base64
DATABASE_BLIND_INDEX_KEY=your_generated_32_byte_key_base64
```

(Never commit your `.env` file to version control. Use `.env.example` to track required keys — with placeholder, non-functional values only, never real generated keys.)