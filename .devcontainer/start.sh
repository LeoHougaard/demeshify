#!/usr/bin/env bash
set -euo pipefail

if curl --fail --silent http://127.0.0.1:8421/api/health >/dev/null 2>&1; then
  exit 0
fi

nohup python -m uv run uvicorn app.main:app --host 0.0.0.0 --port 8421 \
  >/tmp/stl-to-step-converter.log 2>&1 &
