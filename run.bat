@echo off
REM Start Invoice Desk locally on http://localhost:8000
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
if not exist .env copy .env.example .env
echo Invoice Desk: http://localhost:8000
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
