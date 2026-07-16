# Secure Teleconsultation Workflow (LiveKit)

A secure, healthcare-compliant video conferencing workflow built with **LiveKit**, **FastAPI** (Python), **PostgreSQL**, and **React** (Vite). This repository provides a robust foundation for teleconsultation applications, emphasizing strict access control, auditability, and zero-trust principles.

## 🏗️ Architecture & Tech Stack

* **Frontend:** React, Vite, LiveKit Components
* **Backend:** Python, FastAPI, LiveKit Server SDK
* **Database:** PostgreSQL, SQLAlchemy (async), Alembic migrations
* **WebRTC Infrastructure:** LiveKit Server
* **Security Focus:** Short-lived JWTs, strict room isolation, and comprehensive audit logging for compliance tracking.

---

## 🚀 Prerequisites

Before you begin, ensure you have the following installed on your system:

* [Node.js](https://nodejs.org/) (v16 or higher)
* [Python 3.10+](https://www.python.org/)
* [PostgreSQL 15+](https://www.postgresql.org/download/)
* [LiveKit Server](https://docs.livekit.io/realtime/self-hosting/local/) (or the LiveKit CLI installed locally)

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
│   ├── main.py            # FastAPI app, token generation, webhook handling
│   └── requirements.txt
├── frontend/              # React/Vite application UI
├── livekit/                # LiveKit server configuration (.yaml)
├── start-dev.ps1            # Launches all services at once
└── README.md
```

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

### 2. Configure the connection string

Add `DATABASE_URL` to `backend/.env` (see [Environment Variables](#-environment-variables) below).

### 3. Run the migrations

From inside `backend/`, with the virtual environment activated and alembic pip installed:

```powershell
alembic upgrade head
```

This creates all four tables: `consultations`, `waiting_room_entries`, `audit_events`, `processed_webhook_events`.

### 4. Verify

```powershell
psql "postgresql://postgres:<password>@localhost:5432/tachafy_teleconsult" -c "\dt"
```

You should see the four tables listed above.

> **Note:** whenever `backend/models.py` changes, generate a new migration with `alembic revision --autogenerate -m "description"` and re-run `alembic upgrade head` — don't edit the database by hand.

---

## 🚦 Running the Application Locally

To run the full workflow, you will need to open four separate terminal instances: PostgreSQL (usually already running as a background service), the WebRTC server, the backend API, and the frontend client.

### 1. Ensure PostgreSQL is running

On Windows, check via `services.msc` (look for `postgresql-x64-<version>`) — it typically starts automatically. On Linux:

```bash
sudo systemctl start postgresql
```

If you haven't already, complete the [Database Setup](#-database-setup-postgresql) steps above before continuing.

### 2. Start the LiveKit Server

In your next terminal, launch the local LiveKit server using your configuration file:

```powershell
livekit-server --config livekit/livekit.yaml
```

### 3. Start the Backend (FastAPI)

In your next terminal, navigate to the backend directory, activate your virtual environment, and start the API server. Ensure your `.env` file is properly configured with your LiveKit API Key/Secret **and** your `DATABASE_URL`.

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

This assumes dependencies have already been installed at least once (`pip install -r requirements.txt` and `npm install`), that a `venv` exists in `backend/`, that PostgreSQL is running, and that migrations have already been applied (`alembic upgrade head`).

---

## 🛡️ Security & Healthcare Compliance Features

This workflow is designed with healthcare and enterprise security requirements in mind:

* **Ephemeral Rooms:** Rooms are dynamically created for specific consultations and automatically destroyed when empty or expired.
* **Strict Role-Based Access Control (RBAC):** Access tokens are generated with specific LiveKit Video Grants, ensuring patients, doctors, and observers have strictly defined permissions (e.g., publish vs. subscribe-only).
* **Short-Lived Tokens:** JWTs are minted with a low Time-To-Live (TTL) to minimize the attack surface in case of token interception.
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
```

(Never commit your `.env` file to version control. Use `.env.example` to track required keys.)
