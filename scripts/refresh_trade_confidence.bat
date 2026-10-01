@echo off
REM Windows Task Scheduler entry point for scripts/refresh_trade_confidence.py.
REM Registered via: schtasks /create ... Logs to data\refresh_trade_confidence.log
REM so a run's output is checkable without digging through Task Scheduler's own UI.
cd /d "C:\Users\iavit\OneDrive\Documents\undertow"
echo [%date% %time%] tick >> data\refresh_trade_confidence.log
".venv\Scripts\python.exe" scripts\refresh_trade_confidence.py >> data\refresh_trade_confidence.log 2>&1
