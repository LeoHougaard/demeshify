#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_root"

listen_address="${STL_TO_STEP_HOST:-127.0.0.1}"
port="${STL_TO_STEP_PORT:-8421}"

if command -v uv >/dev/null 2>&1; then
  uv_command=(uv)
elif command -v python3 >/dev/null 2>&1; then
  echo "Installing uv for your user account..."
  python3 -m pip install --user uv
  uv_command=(python3 -m uv)
else
  echo "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this script again." >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Install Node.js 22 LTS from https://nodejs.org/ and run this script again." >&2
  exit 1
fi
node_major="$(node --version | sed 's/^v//' | cut -d. -f1)"
if (( node_major < 22 )); then
  echo "Node.js 22 or newer is required. Found $(node --version)." >&2
  exit 1
fi

"${uv_command[@]}" python install 3.12
"${uv_command[@]}" sync --locked
npm --prefix web ci
npm --prefix web run build

echo "STL to STEP Converter is ready at http://${listen_address}:${port}"
exec "${uv_command[@]}" run uvicorn app.main:app --host "$listen_address" --port "$port"
