@echo off
REM Windows Task Scheduler entry point for scripts/run_watchlist_agent.py.
REM Registered via: schtasks /create ... (see PROJECT_PLAN.md "Running the
REM watchlist agent" section). Logs to data\watchlist_agent.log so a run's
REM output is checkable without digging through Task Scheduler's own UI.
cd /d "C:\Users\iavit\OneDrive\Documents\undertow"
echo [%date% %time%] tick >> data\watchlist_agent.log
".venv\Scripts\python.exe" scripts\run_watchlist_agent.py >> data\watchlist_agent.log 2>&1
