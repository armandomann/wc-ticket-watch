#!/bin/bash
# Local fallback runner (use if GitHub's cloud IP gets blocked by StubHub).
# Loads config.env and runs one check. Pair with launchd (see README) to run
# every 30 minutes while your Mac is awake.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f config.env ]; then
  set -a; . ./config.env; set +a
fi
exec python3 check.py
