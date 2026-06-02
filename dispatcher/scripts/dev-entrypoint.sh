#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="${VENV_DIR:-/opt/venv}"
REQ_FILE="${REQ_FILE:-/app/requirements.txt}"
REQ_HASH_FILE="${VENV_DIR}/.requirements.sha256"

export VIRTUAL_ENV="${VENV_DIR}"
export PATH="${VENV_DIR}/bin:${PATH}"

echo "[dispatcher-dev] preparing virtualenv at ${VENV_DIR}"
mkdir -p "${VENV_DIR}"

created_venv=0
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "[dispatcher-dev] creating virtualenv"
  python -m venv "${VENV_DIR}"
  created_venv=1
fi

if [[ "${created_venv}" == "1" ]]; then
  echo "[dispatcher-dev] bootstrapping pip/setuptools/wheel"
  python -m pip install --upgrade pip setuptools wheel >/dev/null
else
  echo "[dispatcher-dev] virtualenv exists, skip bootstrap upgrade"
fi

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "[dispatcher-dev] requirements file not found: ${REQ_FILE}" >&2
  exit 1
fi

current_hash="$(sha256sum "${REQ_FILE}" | awk '{print $1}')"
saved_hash=""
if [[ -f "${REQ_HASH_FILE}" ]]; then
  saved_hash="$(cat "${REQ_HASH_FILE}" || true)"
fi

if [[ "${current_hash}" != "${saved_hash}" ]]; then
  echo "[dispatcher-dev] installing python dependencies"
  pip install -r "${REQ_FILE}"
  echo "${current_hash}" > "${REQ_HASH_FILE}"
else
  echo "[dispatcher-dev] dependencies unchanged, skip pip install"
fi

echo "[dispatcher-dev] starting uvicorn (reload)"
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
