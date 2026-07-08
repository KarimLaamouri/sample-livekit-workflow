<#
    start-dev.ps1
    Launches the LiveKit server, FastAPI backend, and Vite frontend
    each in their own PowerShell window.

    Usage:
        Run from the repo root:
        .\start-dev.ps1

    Notes:
        - Adjust $RootPath below if you run this script from somewhere else.
        - Assumes venv is at backend\venv (Windows layout: venv\Scripts\Activate.ps1)
        - Assumes livekit.yaml is at livekit\livekit.yaml
#>

# Root of the repo (folder containing backend/, frontend/, livekit/)
$RootPath = $PSScriptRoot

# --- 1. LiveKit Server ---
$liveKitCmd = @"
cd '$RootPath'
livekit-server --config livekit/livekit.yaml
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $liveKitCmd

# --- 2. Backend (FastAPI) ---
$backendCmd = @"
cd '$RootPath\backend'
.\venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000 --env-file .env
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# --- 3. Frontend (Vite) ---
$frontendCmd = @"
cd '$RootPath\frontend'
npm run dev
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

Write-Host "Launched LiveKit server, backend, and frontend in separate windows." -ForegroundColor Green