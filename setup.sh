#!/usr/bin/env bash
# Create the virtualenv and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Error: $PYTHON not found. Install Python 3.10 or newer." >&2
  exit 1
fi

echo "Creating virtualenv in .venv ..."
"$PYTHON" -m venv .venv

echo "Installing dependencies ..."
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

chmod +x run-mcp.sh run-panel.sh setup.sh 2>/dev/null || true
mkdir -p cache

echo
echo "Setup complete."
echo "Start the panel:   ./run-panel.sh"
echo "Start MCP server:  ./run-mcp.sh  (stdio; launched by the client)"