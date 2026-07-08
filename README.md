# Secure Teleconsultation Workflow (LiveKit)

A secure, healthcare-compliant video conferencing workflow built with **LiveKit**, **FastAPI** (Python), and **React** (Vite). This repository provides a robust foundation for teleconsultation applications, emphasizing strict access control, auditability, and zero-trust principles.

## 🏗️ Architecture & Tech Stack

* **Frontend:** React, Vite, LiveKit Components
* **Backend:** Python, FastAPI, LiveKit Server SDK
* **WebRTC Infrastructure:** LiveKit Server
* **Security Focus:** Short-lived JWTs, strict room isolation, and comprehensive audit logging for compliance tracking.

---

## 🚀 Prerequisites

Before you begin, ensure you have the following installed on your system:
* [Node.js](https://nodejs.org/) (v16 or higher)
* [Python 3.10+](https://www.python.org/)
* [LiveKit Server](https://docs.livekit.io/realtime/self-hosting/local/) (or the LiveKit CLI installed locally)

---

## 🛠️ Project Structure

```text
sample-livekit-workflow/
├── backend/         # FastAPI server, token generation, webhook handling
├── frontend/        # React/Vite application UI
├── livekit/         # LiveKit server configuration (.yaml)
├── start-dev.ps1    # Launches all three services at once
└── README.md
```

## 🚦 Running the Application Locally

To run the full workflow, you will need to open three separate terminal instances to run the WebRTC server, the backend API, and the frontend client simultaneously.

### 1. Start the LiveKit Server

In your first terminal, launch the local LiveKit server using your configuration file:

```powershell
livekit-server --config livekit/livekit.yaml
```

### 2. Start the Backend (FastAPI)

In your second terminal, navigate to the backend directory, activate your virtual environment, and start the API server. Ensure your `.env` file is properly configured with your LiveKit API Key and Secret.

```powershell
cd backend
# Activate your virtual environment (Windows)
.\venv\Scripts\Activate.ps1
# Install the Python dependencies
pip install -r requirements.txt
# Start the server
uvicorn main:app --reload --port 8000 --env-file .env
```

### 3. Start the Frontend (Vite)

In your third terminal, navigate to the frontend directory and start the Vite development server:

```powershell
cd frontend
# Install dependencies if you haven't already
npm install
# Start the frontend app
npm run dev
```

### ⚡ One-Command Launch

A `start-dev.ps1` script is included in the repo root to launch all three services (LiveKit server, backend, and frontend) at once, each in its own PowerShell window.

```powershell
.\start-dev.ps1
```

This assumes dependencies have already been installed at least once (`pip install -r requirements.txt` and `npm install`), and that a `venv` exists in `backend/`.

---

## 🛡️ Security & Healthcare Compliance Features

This workflow is designed with healthcare and enterprise security requirements in mind:

* **Ephemeral Rooms:** Rooms are dynamically created for specific consultations and automatically destroyed when empty or expired.
* **Strict Role-Based Access Control (RBAC):** Access tokens are generated with specific LiveKit Video Grants, ensuring patients, doctors, and observers have strictly defined permissions (e.g., publish vs. subscribe-only).
* **Short-Lived Tokens:** JWTs are minted with a low Time-To-Live (TTL) to minimize the attack surface in case of token interception.
* **Comprehensive Audit Logging:** The backend tracks and logs critical room events (creation, token issuance, participant joining/leaving, room termination) to maintain compliance trails.

## 🔒 Environment Variables

You must create a `.env` file in the `backend/` directory with the following variables:

```
LIVEKIT_API_URL=http://localhost:7880
LIVEKIT_API_KEY=your_dev_key
LIVEKIT_API_SECRET=your_dev_secret
```

(Never commit your `.env` file to version control. Use `.env.example` to track required keys).