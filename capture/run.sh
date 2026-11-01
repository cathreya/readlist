#!/bin/zsh
# Start the Flashcap server using this repository's virtualenv.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "No .venv found. Create one with: uv sync" >&2
  exit 1
fi

exec .venv/bin/python -m capture.server
