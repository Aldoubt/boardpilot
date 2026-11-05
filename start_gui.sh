#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    printf '缺少 Tk。Ubuntu 上先执行：sudo apt install python3-tk python3-venv\n'
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv --system-site-packages .venv
fi
if ! .venv/bin/python -c 'import paramiko' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python deployer_gui.py "$@"
