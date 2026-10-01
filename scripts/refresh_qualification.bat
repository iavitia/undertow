@echo off
REM Windows Task Scheduler entry point for scripts/refresh_qualification.py.
REM Registered via: schtasks /create ... (see PROJECT_PLAN.md). Logs to
REM data\refresh_qualification.log so a run's output is checkable without
REM digging through Task Scheduler's own UI.
cd /d "C:\Users\iavit\OneDrive\Documents\undertow"
echo [%date% %time%] tick >> data\refresh_qualification.log
".venv\Scripts\python.exe" scripts\refresh_qualification.py >> data\refresh_qualification.log 2>&1
