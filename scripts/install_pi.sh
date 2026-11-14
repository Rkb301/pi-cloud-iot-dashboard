#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${EUID}" -eq 0 ]]; then echo 'Run as the normal Pi user, not root.' >&2; exit 1; fi
if [[ "$(cat /sys/firmware/devicetree/base/model 2>/dev/null | tr -d '\0')" != *'Raspberry Pi'* ]]; then
  echo 'Not a Raspberry Pi. For laptop setup use requirements.txt; see README.' >&2; exit 2
fi
if ! python3 -c 'import venv, lgpio' 2>/dev/null; then
  echo 'Missing system prerequisites. After approval, install python3-venv and python3-lgpio using the Pi package manager.' >&2
  exit 2
fi
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-pi.txt
.venv/bin/python -m pip check
if [[ ! -f .env ]]; then (umask 077; cp .env.example .env); fi
chmod 600 .env
echo 'Virtual environment installed. No system packages, GPIO, cloud writes or services changed.'
