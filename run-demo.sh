#!/usr/bin/env bash
set -euo pipefail
pit_project_dir="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${PIT_MEMORY_PYTHON:-}" ]]; then
  pit_python="$PIT_MEMORY_PYTHON"
elif [[ -x "$pit_project_dir/.venv/bin/python" ]]; then
  pit_python="$pit_project_dir/.venv/bin/python"
else
  pit_python=""
  for candidate in python3 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      pit_python="$candidate"
      break
    fi
  done
  if [[ -z "$pit_python" ]]; then
    echo "run-demo.sh: Python 3.11 or newer is required; set PIT_MEMORY_PYTHON to its path" >&2
    exit 2
  fi
fi
PYTHONPATH="$pit_project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$pit_python" -m pit_memory demo --output "$pit_project_dir/runs/demo" "$@"
