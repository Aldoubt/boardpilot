#!/usr/bin/env bash
# Install an unpacked SunrayV2 tree onto ~/Sunray_v2.
# Gitignored files are neither copied nor deleted. Existing third-party
# Git metadata and ignored build trees on the target stay where they are.

set -euo pipefail

readonly SUNRAY_APPLY_SAFETY_VERSION=1

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
    cat <<'EOF'
用法：tools/workspace_transfer/apply_workspace.sh [选项]

在目标机器上，把已经解开的 SunrayV2 覆盖到 ~/Sunray_v2。解压目录可以在任何位置。
包里的普通源码会覆盖目标上的同名文件。目标上被 .gitignore 命中的已有文件保持不动，
包括 build/、devel/、install/、logs/，以及 thirdparty/draco、livox_sdk2、librealsense。
包里没有、目标上有、且没有被忽略的文件会删掉。
所有层级的 .git 文件和目录始终保留。包里缺少目标已有的已声明子模块源码时拒绝安装，
请先在开发机补齐子模块，再重新打包。

不要在正在使用的 ~/Sunray_v2 里面执行。源目录和目标目录相同，或者一方包含另一方时会退出。

先说要复制和删除多少，再问 yes 或 no。回答 no，或者还没回答就失败，都不会改目标。
sunray-system、sunray-clock-helper 只在回答 yes 之后停掉正在运行的，密码只问一次。
装完不自动启动，也不编译。没有终端时才需要 --yes。

选项：
      --dest 目录        安装位置，默认是当前用户的 ~/Sunray_v2
      --yes              不再询问。有改动时，正在运行的服务也会停掉
      --dry-run          只打印计划，不询问，不改目标，也不停服务
  -h, --help             显示这份说明

--stop-services 是旧写法，现在可以不写。

示例：
  cd /tmp/Sunray_v2
  tools/workspace_transfer/apply_workspace.sh
  tools/workspace_transfer/apply_workspace.sh --dry-run
  tools/workspace_transfer/apply_workspace.sh --yes
EOF
}

die() {
    printf '[apply] %s\n' "$*" >&2
    exit 2
}

dry_run=0
assume_yes=0
dest=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --yes) assume_yes=1 ;;
        --stop-services) ;;
        -h|--help) usage; exit 0 ;;
        --dest)
            [[ $# -ge 2 ]] || die "--dest 需要目录"
            dest=$2
            shift
            ;;
        --dest=*) dest=${1#--dest=} ;;
        --) shift; break ;;
        -*) die "未知选项：$1（用 --help 查看写法）" ;;
        *) die "这个脚本不接受位置参数：$1" ;;
    esac
    shift
done

[[ -f "$ROOT/tools/workspace_transfer/apply_workspace.sh" ]] || die "请在解压出来的 SunrayV2 根目录里执行"
if [[ -z "$dest" ]]; then
    dest="${HOME}/Sunray_v2"
fi
dest="${dest/#\~/$HOME}"
[[ "$dest" == /* ]] || die "目标必须是绝对路径：$dest"

source_real="$(cd "$ROOT" && pwd -P)"
if [[ -d "$dest" ]]; then
    dest_real="$(cd "$dest" && pwd -P)"
else
    mkdir -p "$(dirname "$dest")"
    dest_parent="$(cd "$(dirname "$dest")" && pwd -P)"
    dest_real="${dest_parent}/$(basename "$dest")"
fi
[[ "$source_real" != "$dest_real" ]] || die "源目录和目标目录是同一个：$source_real"
case "$dest_real/" in
    "$source_real/"*) die "目标在源目录里面，不能覆盖：$dest_real" ;;
esac
case "$source_real/" in
    "$dest_real/"*) die "源目录在目标里面，不能覆盖：$source_real" ;;
esac

service_active() {
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$1"
}
running_units=()
service_active sunray-system.service && running_units+=("sunray-system")
service_active sunray-clock-helper.service && running_units+=("sunray-clock-helper")

printf '[apply] 源：%s\n' "$source_real"
printf '[apply] 目标：%s\n' "$dest_real"
printf '[apply] 正在统计…\n'

export APPLY_SOURCE="$source_real"
export APPLY_DEST="$dest_real"
export APPLY_DRY_RUN="$dry_run"
export APPLY_YES="$assume_yes"
apply_services=""
if [[ ${#running_units[@]} -gt 0 ]]; then
    apply_services="${running_units[*]}"
fi
export APPLY_SERVICES="$apply_services"

python3 - <<'PY'
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

source = Path(os.environ["APPLY_SOURCE"])
dest = Path(os.environ["APPLY_DEST"])
dry_run = os.environ["APPLY_DRY_RUN"] == "1"
assume_yes = os.environ["APPLY_YES"] == "1"
services = [name for name in os.environ["APPLY_SERVICES"].split() if name]
interactive = sys.stderr.isatty()

state = {
    "phase": "统计",
    "checked": 0,
    "total": 1,
    "current": "清点文件",
    "done": False,
    "started": time.time(),
}
lock = threading.Lock()

def draw(frame, final=False):
    if not interactive and not final:
        return
    with lock:
        phase, checked, total, current, started = (
            state["phase"], state["checked"], state["total"], state["current"], state["started"]
        )
    ratio = 1.0 if final else min(1.0, checked / total)
    width = 28
    filled = int(width * ratio)
    bar = "█" * filled + "░" * (width - filled)
    blink = "●" if frame % 2 else "○"
    elapsed = int(time.time() - started)
    clock = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
    shown = current if len(current) <= 52 else "…" + current[-51:]
    sys.stderr.write(
        f"\r\033[K{blink} {phase} {ratio * 100:5.1f}% [{bar}] {checked} 个  {clock}  {shown}"
    )
    sys.stderr.flush()

def blink():
    frame = 0
    while not state["done"]:
        draw(frame)
        frame += 1
        time.sleep(0.2)

def bump(phase, checked, total, current):
    with lock:
        state["phase"] = phase
        state["checked"] = checked
        state["total"] = max(1, total)
        state["current"] = current

thread = None

def finish_progress(current, success):
    state["done"] = True
    if not interactive or thread is None:
        return
    thread.join(timeout=1)
    with lock:
        if success:
            state["checked"] = state["total"]
        state["current"] = current
    draw(1, final=True)
    sys.stderr.write("\n")
    sys.stderr.flush()

def is_git_metadata(rel):
    return ".git" in PurePosixPath(rel).parts


def collect(root, rule_root=None):
    if not root.exists():
        return []
    found = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        candidates = []
        for name in dirnames:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if is_git_metadata(rel):
                continue
            if path.is_symlink():
                found.append(rel)
            else:
                candidates.append(rel)
        # 目录已经被上层 .gitignore 排除时不再进去。node_modules 里各包自己的
        # .gitignore 不会再读。
        ignored_dirs = ignored_among(rule_root, candidates) if rule_root is not None else set()
        dirnames[:] = [rel.rsplit("/", 1)[-1] for rel in candidates if rel not in ignored_dirs]
        for name in filenames:
            rel = (current_path / name).relative_to(root).as_posix()
            if is_git_metadata(rel):
                continue
            found.append(rel)
        if len(found) % 200 == 0:
            bump("统计", len(found), max(len(found), 1), root.name)
    return found

ignore_git_dirs = {}


@atexit.register
def clean_ignore_git_dirs():
    for directory in ignore_git_dirs.values():
        shutil.rmtree(directory, ignore_errors=True)

def ignored_among(rule_root, paths):
    if not paths:
        return set()
    git_dir = ignore_git_dirs.get(rule_root)
    if git_dir is None:
        git_dir = tempfile.mkdtemp(prefix="sunray-apply-git.")
        subprocess.run(["git", f"--git-dir={git_dir}", "init", "-q"], check=True)
        ignore_git_dirs[rule_root] = git_dir
    ignored = set()
    seen = 0

    def flush(items):
        nonlocal seen
        if not items:
            return
        payload = "\0".join(items).encode() + b"\0"
        result = subprocess.run(
            [
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={rule_root}",
                "check-ignore",
                "--stdin",
                "-v",
                "-z",
                "--no-index",
            ],
            input=payload,
            stdout=subprocess.PIPE,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise SystemExit(result.returncode)
        fields = result.stdout.split(b"\0")
        for index in range(0, len(fields) - 3, 4):
            pattern = fields[index + 2].decode()
            pathname = fields[index + 3].decode()
            if pattern and not pattern.startswith("!"):
                ignored.add(pathname)
        seen += len(items)
        bump("统计", seen, len(paths), items[-1])

    chunk = []
    for path in paths:
        chunk.append(path)
        if len(chunk) == 500:
            flush(chunk)
            chunk = []
    flush(chunk)
    return ignored


def declared_submodules(root):
    path = root / ".gitmodules"
    if not path.is_file():
        return []
    result = subprocess.run(
        ["git", "config", "--null", "--file", str(path), "--get-regexp",
         r"^submodule\..*\.path$"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode not in (0, 1):
        print(f"[apply] 没装。无法读取子模块清单：{path}")
        raise SystemExit(2)
    modules = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        _, value = entry.split(b"\n", 1)
        rel = value.decode("utf-8")
        parts = PurePosixPath(rel).parts
        if not parts or PurePosixPath(rel).is_absolute() or ".." in parts or ".git" in parts or "\\" in rel:
            print(f"[apply] 没装。子模块路径非法：{rel!r}")
            raise SystemExit(2)
        modules.append(PurePosixPath(rel).as_posix())
    return modules


def module_payload(files, module):
    prefix = module + "/"
    # A placeholder containing only Git bookkeeping does not constitute a checkout.
    return [name for name in files if name.startswith(prefix)
            and name[len(prefix):] not in (".gitignore", ".gitmodules")]


def validate_submodule_payloads(source_files, target_files):
    missing = []
    visited = set()

    def visit(root, prefix=""):
        real_root = root.resolve()
        if real_root in visited:
            return
        visited.add(real_root)
        for rel in declared_submodules(root):
            module = prefix + rel
            payload = module_payload(source_files, module)
            target_payload = module_payload(target_files, module)
            if not payload and target_payload:
                missing.append((module, len(target_payload)))
            elif payload:
                visit(source / module, module + "/")

    visit(source)
    if missing:
        print("[apply] 没装。部署包缺少以下目标已有的子模块源码；继续会删除这些源码：")
        for module, count in missing:
            print(f"[apply]   {module}（目标 {count} 个文件，包内无源码）")
        print("[apply] 请在开发机将这些子模块检出到主仓库记录的版本，重新生成并上传部署包。")
        raise SystemExit(2)


def contains_git_metadata(path):
    if path.is_symlink() or not path.is_dir():
        return False
    for _, dirnames, filenames in os.walk(path, followlinks=False):
        if ".git" in dirnames or ".git" in filenames:
            return True
    return False


def validate_metadata_protection(copy_files, delete_files):
    if any(is_git_metadata(rel) for rel in copy_files + delete_files):
        print("[apply] 没装。计划包含 Git 元数据路径，拒绝执行。")
        raise SystemExit(2)
    for rel in copy_files + delete_files:
        # A source file/symlink can replace an existing directory. Reject it before
        # stopping services if that replacement would remove nested Git metadata.
        if contains_git_metadata(dest / rel):
            print(f"[apply] 没装。替换目录会删除其中的 Git 元数据：{rel}")
            raise SystemExit(2)

def open_terminal():
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None, None
    reader = os.fdopen(os.dup(fd), "r", buffering=1, closefd=True)
    writer = os.fdopen(fd, "w", buffering=1, closefd=True)
    return reader, writer

def print_summary():
    action = "新建" if not dest.exists() else "覆盖"
    print(f"[apply] {action} {dest}")
    print(f"[apply] 复制 {len(copy_files)} 个，删除 {len(delete_files)} 个。被忽略的文件不动。")
    if services and (copy_files or delete_files):
        print(f"[apply] 回答 yes 后会先停止 {' '.join(services)}。")

def confirm():
    if assume_yes or dry_run:
        return
    reader, writer = open_terminal()
    if reader is None:
        print("[apply] 没装。当前没有终端，请在终端里运行，或加上 --yes。")
        raise SystemExit(2)
    try:
        while True:
            writer.write("安装？[yes/no] ")
            writer.flush()
            answer = reader.readline()
            if not answer:
                print("[apply] 没装。")
                raise SystemExit(1)
            word = answer.strip().lower()
            if word in {"yes", "y"}:
                return
            if word in {"no", "n"}:
                print("[apply] 没装。")
                raise SystemExit(1)
            writer.write("请输入 yes 或 no。\n")
            writer.flush()
    finally:
        reader.close()
        writer.close()

def run_stop(command, tty):
    try:
        return subprocess.run(command, stdin=tty, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127)

def mark_stopped():
    global services_stopped
    services_stopped = True

def stop_services():
    if not services or not (copy_files or delete_files):
        return
    units = [f"{name}.service" for name in services]
    print(f"[apply] 停止 {' '.join(services)}")
    if run_stop(["systemctl", "stop", "--no-ask-password", *units], subprocess.DEVNULL).returncode == 0:
        mark_stopped()
        return
    if shutil.which("sudo"):
        if run_stop(["sudo", "-n", "systemctl", "stop", *units], subprocess.DEVNULL).returncode == 0:
            mark_stopped()
            return
        print("[apply] 停止服务需要一次密码。")
        if subprocess.run(["sudo", "systemctl", "stop", *units]).returncode == 0:
            mark_stopped()
            return
        print("[apply] 没装。服务没停下来，文件没改。")
        raise SystemExit(1)
    if shutil.which("pkexec") and subprocess.run(["pkexec", "systemctl", "stop", *units]).returncode == 0:
        mark_stopped()
        return
    print("[apply] 没装。停不了服务，文件没改。")
    raise SystemExit(1)

scan_ok = False
try:
    source_files = collect(source, source)
    bump("统计", 0, max(1, len(source_files)), "比对源目录的忽略规则")
    source_ignored = ignored_among(source, source_files)
    copy_files = [path for path in source_files if path not in source_ignored]
    copy_set = set(copy_files)
    dest_files = collect(dest, source)
    bump("统计", 0, max(1, len(dest_files)), "比对目标目录的忽略规则")
    dest_ignored = ignored_among(source, dest_files)
    delete_files = [
        path for path in dest_files if path not in copy_set and path not in dest_ignored
    ]
    validate_submodule_payloads(copy_files, [path for path in dest_files if path not in dest_ignored])
    validate_metadata_protection(copy_files, delete_files)
    scan_ok = True
finally:
    if not scan_ok:
        print("[apply] 没装。统计中断。")

print_summary()
if not copy_files and not delete_files:
    print("[apply] 没有要改的文件。")
    raise SystemExit(0)
if dry_run:
    print("[apply] 这是预览，没装。")
    raise SystemExit(0)

confirm()
services_stopped = False
stop_services()

state = {
    "phase": "传输" if copy_files else "删除",
    "checked": 0,
    "total": max(1, len(copy_files) or len(delete_files)),
    "current": "准备目录",
    "done": False,
    "started": time.time(),
}
thread = threading.Thread(target=blink, daemon=True)
if interactive:
    thread.start()

def abort_copy(message, code):
    finish_progress("中断", False)
    print(message, file=sys.stderr)
    if services_stopped:
        print("[apply] 服务已停，没有自动启动。目标可能不完整，重新执行可以继续。", file=sys.stderr)
    raise SystemExit(code)

try:
    dest.mkdir(parents=True, exist_ok=True)
    total = len(copy_files)
    for index, rel in enumerate(copy_files, start=1):
        origin = source / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir():
            if contains_git_metadata(target):
                raise RuntimeError(f"拒绝删除包含 Git 元数据的目录：{rel}")
            shutil.rmtree(target)
        if origin.is_symlink():
            os.symlink(os.readlink(origin), target)
        else:
            shutil.copy2(origin, target, follow_symlinks=False)
        bump("传输", index, total, rel)
    delete_total = len(delete_files)
    for index, rel in enumerate(delete_files, start=1):
        target = dest / rel
        if target.is_dir() and not target.is_symlink():
            if contains_git_metadata(target):
                raise RuntimeError(f"拒绝删除包含 Git 元数据的目录：{rel}")
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        bump("删除", index, delete_total or 1, rel)
except KeyboardInterrupt:
    abort_copy("[apply] 已中断。", 130)
except Exception as exc:
    abort_copy(f"[apply] 覆盖中断：{exc}", 1)
finish_progress("完成", True)
print(f"[apply] 已安装到 {dest}")
if services_stopped:
    print(f"[apply] {' '.join(services)} 仍是停止状态。")
PY
