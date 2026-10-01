#!/bin/sh
# Reference only -- run this by hand in your own terminal if you like.
#
# If Claude is launching this (not a human), do NOT invoke it via
# `bash scripts/dev_api.sh`: on this Windows/git-bash setup, any script
# wrapper (plain, or even `exec`'d) leaves uvicorn running as an orphan
# once the harness stops the wrapping shell task -- confirmed empirically,
# multiple ways, this session. Invoke the command below directly as the
# Bash tool's command instead; only then does the harness's TaskStop
# reliably kill the actual process, since it's tracking uvicorn's own PID
# with no wrapper in between.
#
# uvicorn's --reload was also tried and dropped: on this setup its
# multiprocessing worker respawns under the wrong Python interpreter
# (system pythoncore, not .venv, so it can't import fastapi/pandas/etc.)
# and leaves its own orphaned process tree regardless of the wrapper issue
# above. Restart by stopping and relaunching instead.
cd "$(dirname "$0")/.."
.venv/Scripts/python.exe -m uvicorn api.main:app --port 8000
