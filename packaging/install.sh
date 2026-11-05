#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
target="$data/boardpilot"
mkdir -p "$target" "$data/applications" "$data/icons/hicolor/scalable/apps"
install -m 755 BoardPilot "$target/BoardPilot"
install -m 755 uninstall.sh "$target/uninstall.sh"
install -m 644 boardpilot.svg "$data/icons/hicolor/scalable/apps/boardpilot.svg"
python3 - "$target" "$data/applications/boardpilot.desktop" <<'PY'
import pathlib,sys
exe=str(pathlib.Path(sys.argv[1]) / 'BoardPilot')
exe=exe.replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$').replace('%','%%')
text=pathlib.Path('boardpilot.desktop').read_text().replace('Exec=BoardPilot','Exec="'+exe+'"')
pathlib.Path(sys.argv[2]).write_text(text)
PY
if command -v update-desktop-database >/dev/null; then update-desktop-database "$data/applications" || true; fi
printf 'Installed. Open BoardPilot from your applications menu.\n'
