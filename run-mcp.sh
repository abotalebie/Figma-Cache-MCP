#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -r "$HOME/.config/figma-cache-mcp/token" ]]; then
  export FIGMA_TOKEN="$(<"$HOME/.config/figma-cache-mcp/token")"
fi

exec .venv/bin/python server.py
