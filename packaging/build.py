"""Build native desktop bundles on the operating system that will run them."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
os.chdir(ROOT)
subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
    "--onefile", "--windowed", "--name", "BoardPilot",
    "--hidden-import", "remote_helpers", "--hidden-import", "paramiko",
    "deployer_gui.py"], check=True)
out = ROOT / "dist"
if sys.platform == "win32":
    artifact = out / ("BoardPilot-" + VERSION + "-windows-x64.exe")
    shutil.copy2(out / "BoardPilot.exe", artifact)
else:
    folder = out / ("BoardPilot-" + VERSION + "-linux-" + platform.machine())
    folder.mkdir(exist_ok=True)
    shutil.copy2(out / "BoardPilot", folder / "BoardPilot")
    for name in ("install.sh", "uninstall.sh", "boardpilot.desktop", "boardpilot.svg"):
        shutil.copy2(ROOT / "packaging" / name, folder / name)
    for name in ("BoardPilot", "install.sh", "uninstall.sh"):
        (folder / name).chmod(0o755)
    shutil.copy2(ROOT / "README.md", folder / "README.md")
    artifact = Path(shutil.make_archive(str(folder), "gztar", out, folder.name))
checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
Path(str(artifact) + ".sha256").write_text(checksum + "  " + artifact.name + "\n")
print(artifact)
