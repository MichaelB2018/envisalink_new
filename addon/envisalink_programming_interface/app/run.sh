#!/usr/bin/env bash
set -e
cd /srv
exec uvicorn app.server:app --host 0.0.0.0 --port 8099 --log-level info
