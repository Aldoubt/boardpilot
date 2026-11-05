#!/usr/bin/env bash
set -eu
data="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -f -- "$data/applications/boardpilot.desktop" "$data/icons/hicolor/scalable/apps/boardpilot.svg" "$data/boardpilot/BoardPilot" "$data/boardpilot/uninstall.sh"
rmdir -- "$data/boardpilot" 2>/dev/null || true
printf 'Removed application. Your configuration has been kept.\n'
