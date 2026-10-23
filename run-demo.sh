#!/usr/bin/env bash
set -euo pipefail
pit_project_dir="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${PIT_MEMORY_PYTHON:-}" ]]; then
  pit_python="$PIT_MEMORY_PYTHON"
elif [[ -x "$pit_project_dir/.venv/bin/python" ]]; then
  pit_python="$pit_project_dir/.venv/bin/python"
else
  pit_python="python3.11"
fi
PYTHONPATH="$pit_project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$pit_python" -m pit_memory demo --output "$pit_project_dir/runs/demo" "$@"
