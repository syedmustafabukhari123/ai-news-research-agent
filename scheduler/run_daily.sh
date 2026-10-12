#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_FILE="${LOCK_FILE:-${PROJECT_ROOT}/logs/cron.lock}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python executable not found or not executable: ${PYTHON_BIN}" >&2
  echo "Set PYTHON_BIN to the project's virtualenv interpreter." >&2
  exit 2
fi

mkdir -p "$(dirname -- "${LOCK_FILE}")"
cd "${PROJECT_ROOT}"
exec flock -n "${LOCK_FILE}" "${PYTHON_BIN}" -m main
