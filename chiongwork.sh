#!/usr/bin/env sh
# ChiongWork on macOS / Linux: sets itself up on first run, then opens the app in your browser.
# Run:  ./chiongwork.sh          (or pass CLI commands:  ./chiongwork.sh search "data analyst")
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "ChiongWork needs Python 3.11 or newer: https://www.python.org/downloads/" >&2
    exit 1
  fi
  echo "First run: creating a private Python environment in .venv ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
fi

if [ "$#" -eq 0 ]; then
  exec .venv/bin/python -m sgjobs web
else
  exec .venv/bin/python -m sgjobs "$@"
fi
