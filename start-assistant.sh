#!/bin/bash
# Start the Claude Desktop bridge that powers the website's assistant.
#
#   ./start-assistant.sh
#
# Leave this running. The website talks to it on 127.0.0.1:8765 through its own
# server-side proxy, so nothing here is exposed to the browser.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv-bridge/bin/python ]; then
  echo "Setting up the bridge's Python environment (one time)…"
  python3.11 -m venv .venv-bridge
  .venv-bridge/bin/pip install --quiet --upgrade pip
  .venv-bridge/bin/pip install --quiet -r bridge/requirements.txt
fi

echo "Claude Desktop must be running with a conversation open."
exec .venv-bridge/bin/python -m bridge.service
