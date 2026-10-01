@echo off
REM Windows Task Scheduler entry point for scripts/run_live_agent.py.
REM Registered via: schtasks /create ... (same pattern as
REM run_watchlist_agent.bat). Logs to data\live_agent.log so a run's
REM output is checkable without digging through Task Scheduler's own UI.
REM Gated by config.EXECUTION_ENABLED (.env) -- the script itself refuses
REM to place any order if that's not explicitly true, regardless of this
REM task running on schedule.
cd /d "C:\Users\iavit\OneDrive\Documents\undertow"
echo [%date% %time%] tick >> data\live_agent.log
".venv\Scripts\python.exe" scripts\run_live_agent.py >> data\live_agent.log 2>&1
