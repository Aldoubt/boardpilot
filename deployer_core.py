"""Portable source packaging and SSH staging. Python >= 3.8; never builds locally."""
import base64
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

MANIFEST = '.boardpilot-manifest.json'
PROTECTED = ('build', 'devel', 'install', 'logs', '.catkin_tools', '.venv')


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('操作已停止')


def git(root, *args):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    p = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, creationflags=flags, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.decode('utf-8', errors='replace').strip())
    return p.stdout


def inspect_workspace(root):
    root = Path(root).expanduser().resolve()
    top = Path(os.fsdecode(git(root, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if top != root:
        raise ValueError('请选择 Sunray 仓库根目录：' + str(top))
    if not (root / 'build.sh').is_file():
        raise ValueError('所选目录没有 build.sh，不是 Sunray 工作区')
    tracked = git(root, 'ls-files', '--stage', '-z', '--recurse-submodules')
    modes = {}
    for record in tracked.split(b'\0'):
        if not record:
            continue
        fields, path = record.split(b'\t', 1)
        mode, blob, stage = fields.split()
        if stage != b'0':
            raise ValueError('存在未解决的 Git 合并冲突，先解决再打包')
        modes[os.fsdecode(path).replace('\\', '/')] = mode.decode()
    others = git(root, 'ls-files', '-z', '--others', '--exclude-standard')
    names = set(modes) | {os.fsdecode(p).replace('\\', '/') for p in others.split(b'\0') if p}
    files = []
    for name in sorted(names):
        parts = PurePosixPath(name).parts
        if (not parts or name == MANIFEST or '.git' in parts or
                parts[0] in PROTECTED or '..' in parts or PurePosixPath(name).is_absolute() or
                name.startswith('localization/third_party_localization/openvins/ov_data/')):
            continue
        path = root / name
        if path.is_file() or path.is_symlink():
            files.append((name, modes.get(name, '100755' if name.endswith('.sh') else '100644')))
    return {'root': root, 'files': files,
            'commit': git(root, 'rev-parse', 'HEAD').decode().strip(),
            'branch': git(root, 'branch', '--show-current').decode().strip(),
            'status': git(root, 'status', '--short').decode('utf-8', errors='replace'),
            'submodules': git(root, 'submodule', 'status', '--recursive').decode('utf-8', errors='replace')}


def sha256_file(path, cancel=None):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            check_cancel(cancel)
            digest.update(data)
    return digest.hexdigest()


def package_workspace(root, output_dir, log=lambda x: None, progress=lambda a, b: None, cancel=None):
    info = inspect_workspace(root)
    root = info['root']
    output_dir = Path(output_dir).expanduser().resolve()
    # Packages live outside the worktree so an old archive cannot be packed recursively.
    if output_dir == root or root in output_dir.parents:
        raise ValueError('输出目录请放在源码工作区之外，避免把旧压缩包再次打包')
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / ('BoardPilot_%s_%s_%s.tar.gz' %
                           (time.strftime('%Y%m%d_%H%M%S'), info['commit'][:8], uuid.uuid4().hex[:4]))
    temporary = output.with_suffix(output.suffix + '.part')
    normalized = []
    total = len(info['files'])
    log('源码分支 %s / %s；%s 个文件' % (info['branch'], info['commit'][:12], total))
    try:
        with tarfile.open(temporary, 'w:gz', compresslevel=3, dereference=False) as archive:
            for index, (name, mode) in enumerate(info['files'], 1):
                check_cancel(cancel)
                path = root / name
                entry = archive.gettarinfo(str(path), arcname=name)
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ''
                if path.is_symlink() or mode == '120000':
                    target = os.readlink(path) if path.is_symlink() else path.read_text(encoding='utf-8').strip()
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                    if target.startswith('/') or resolved == '..' or resolved.startswith('../'):
                        raise ValueError('符号链接超出源码目录：' + name)
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = target
                    entry.size = 0
                    archive.addfile(entry)
                else:
                    entry.mode = 0o755 if mode == '100755' else 0o644
                    if name.endswith('.sh'):
                        data = path.read_bytes()
                        if b'\r\n' in data:
                            data = data.replace(b'\r\n', b'\n')
                            normalized.append(name)
                        entry.size = len(data)
                        archive.addfile(entry, io.BytesIO(data))
                    else:
                        with path.open('rb') as stream:
                            archive.addfile(entry, stream)
                progress(index, total)
            manifest = {k: v for k, v in info.items() if k not in ('root', 'files')}
            manifest.update(format_version=1, created_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            file_count=total, shell_files_normalized_to_lf=normalized)
            data = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            entry = tarfile.TarInfo(MANIFEST)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
        check_cancel(cancel)
        digest = sha256_file(temporary, cancel)
        temporary.replace(output)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    receipt = {'path': str(output), 'sha256': digest, 'bytes': output.stat().st_size,
               'commit': info['commit'], 'branch': info['branch'], 'file_count': total,
               'dirty': bool(info['status']), 'normalized_shell_files': len(normalized)}
    output.with_name(output.name + '.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    log('打包完成：%.1f MB；SHA256 %s' % (receipt['bytes'] / 1024**2, digest))
    return receipt


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class UnknownHost(Exception):
    def __init__(self, hostname, key):
        self.hostname, self.key = hostname, key
        super().__init__('%s\n%s' % (hostname, fingerprint(key)))


def validate_connection(host, port, user):
    if not re.fullmatch(r'[A-Za-z0-9_.:\-]+', host) or host.startswith('-'):
        raise ValueError('主机请填写 IP 或主机名，不要包含 ssh 命令或路径')
    if not 1 <= int(port) <= 65535:
        raise ValueError('SSH 端口必须在 1—65535 之间')
    if not re.fullmatch(r'[A-Za-z0-9_.\-]+', user) or user.startswith('-'):
        raise ValueError('用户名格式不正确')


class SSHSession:
    def __init__(self, config_dir, log=lambda x: None):
        self.config_dir = Path(config_dir)
        self.client = None
        self.home = None
        self.identity = None
        self.log = log

    def connect(self, host, port, user, password='', keyfile='', trusted=None):
        import paramiko
        validate_connection(host, port, user)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known = Path.home() / '.ssh' / 'known_hosts'
        if known.is_file():
            client.load_host_keys(str(known))
        extra = self.config_dir / 'known_hosts'
        if extra.is_file():
            client.load_host_keys(str(extra))
        class Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(policy_self, cl, hostname, key):
                if trusted is None or hostname != trusted[0] or key != trusted[1]:
                    raise UnknownHost(hostname, key)
                cl.get_host_keys().add(hostname, key.get_name(), key)
                self.config_dir.mkdir(parents=True, exist_ok=True)
                cl.save_host_keys(str(extra))
        client.set_missing_host_key_policy(Policy())
        try:
            client.connect(hostname=host, port=int(port), username=user, password=password or None,
                           key_filename=str(Path(keyfile).expanduser()) if keyfile else None,
                           passphrase=password or None, timeout=8, banner_timeout=12, auth_timeout=12,
                           allow_agent=not bool(password), look_for_keys=not bool(password))
            client.get_transport().set_keepalive(15)
            with client.open_sftp() as sftp:
                home = sftp.normalize('.')
        except BaseException:
            client.close()
            raise
        self.close()
        self.client, self.home, self.identity = client, home, (host, int(port), user)
        return home

    def close(self):
        if self.client:
            self.client.close()
        self.client = None
        self.home = None
        self.identity = None

    def require(self):
        if not self.client or not self.client.get_transport() or not self.client.get_transport().is_active():
            raise RuntimeError('SSH 未连接或已断开，请重新连接')

    def path(self, value):
        self.require()
        if '\n' in value or '\r' in value or '\x00' in value:
            raise ValueError('远程目录不能包含换行')
        if value == '~':
            value = self.home
        elif value.startswith('~/'):
            value = self.home + value[1:]
        if not value.startswith('/'):
            raise ValueError('远程目录请使用 ~/目录 或绝对路径')
        return posixpath.normpath(value)

    def command(self, command, cancel=None, emit=None, on_channel=None, timeout=60):
        self.require()
        channel = self.client.get_transport().open_session(timeout=10)
        if on_channel:
            channel.get_pty(width=140, height=36)
        channel.exec_command(command)
        channel.settimeout(1)
        if on_channel:
            on_channel(channel)
        chunks = []
        start = time.monotonic()
        last_data = start
        try:
            while True:
                check_cancel(cancel)
                consumed = False
                for stderr in (False, True):
                    ready = channel.recv_stderr_ready if stderr else channel.recv_ready
                    recv = channel.recv_stderr if stderr else channel.recv
                    if ready():
                        data = recv(65536)
                        chunks.append(data)
                        if emit:
                            emit(data)
                        last_data = time.monotonic()
                        consumed = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return channel.recv_exit_status(), b''.join(chunks).decode('utf-8', errors='replace')
                if not self.client.get_transport().is_active():
                    raise ConnectionError('SSH 连接已断开')
                if timeout and time.monotonic() - last_data > timeout:
                    raise TimeoutError('远程命令等待超时')
                if not consumed:
                    time.sleep(.04)
        finally:
            if on_channel:
                on_channel(None)
            channel.close()

    def environment(self, cancel=None):
        command = "date -Is; uname -m; cat /etc/os-release | head -6; head -1 /etc/nv_tegra_release 2>/dev/null; g++ --version | head -1; python3 --version; cmake --version | head -1; df -h \"$HOME\"; if [ -f /opt/ros/noetic/setup.bash ]; then bash -c 'source /opt/ros/noetic/setup.bash; rosversion -d'; fi"
        code, text = self.command(command, cancel)
        if code:
            raise RuntimeError(text)
        return text

    def upload(self, package, base, progress=lambda a,b: None, cancel=None):
        self.require()
        package = Path(package)
        digest = sha256_file(package, cancel)
        base = self.path(base)
        if base in ('/', self.home):
            raise ValueError('请选择专用暂存目录，例如 ~/boardpilot_incoming')
        remote = posixpath.join(base, package.name)
        part = remote + '.' + digest[:12] + '.part'
        size = package.stat().st_size
        import paramiko
        sftp = paramiko.SFTPClient.from_transport(self.client.get_transport(), window_size=16*1024*1024)
        sftp.get_channel().settimeout(20)
        try:
            current = '/'
            for component in base.strip('/').split('/'):
                current = posixpath.join(current, component)
                try:
                    attrs = sftp.stat(current)
                    if not stat.S_ISDIR(attrs.st_mode):
                        raise ValueError('远程路径不是目录：' + current)
                except FileNotFoundError:
                    sftp.mkdir(current)
            offset = 0
            try:
                offset = sftp.stat(part).st_size
            except FileNotFoundError:
                pass
            if offset > size:
                raise ValueError('远端未完成文件比本地文件大，拒绝续传')
            if offset:
                with sftp.open(part, 'rb') as existing, package.open('rb') as local:
                    for start in (0, max(0, offset-65536)):
                        check_cancel(cancel)
                        existing.seek(start); local.seek(start)
                        count = min(65536, offset-start)
                        if existing.read(count) != local.read(count):
                            raise ValueError('远端部分文件与本地不一致，拒绝续传')
                self.log('从 %.1f MB 处续传' % (offset/1024**2))
            with package.open('rb') as source, sftp.open(part, 'ab' if offset else 'wb') as dest:
                dest.set_pipelined(True)
                source.seek(offset)
                progress(offset, size)
                for data in iter(lambda: source.read(256*1024), b''):
                    check_cancel(cancel)
                    dest.write(data)
                    offset += len(data)
                    progress(offset, size)
            check_cancel(cancel)
            status, response = self.command('sha256sum -- ' + shlex.quote(part), cancel, timeout=120)
            if status or response.split()[0] != digest:
                raise ValueError('板端 SHA256 不一致，文件保留为 .part，未解压')
            # A unique archive name generated by the packer avoids replacing other releases.
            try:
                sftp.stat(remote)
            except FileNotFoundError:
                sftp.rename(part, remote)
            else:
                code, existing_hash = self.command('sha256sum -- ' + shlex.quote(remote), cancel)
                if code or existing_hash.split()[0] != digest:
                    raise ValueError('远端存在同名不同内容的包，未覆盖')
                sftp.remove(part)
        finally:
            sftp.close()
        return {'archive': remote, 'sha256': digest, 'bytes': size}

    def extract(self, upload, cancel=None):
        from remote_helpers import EXTRACT_CODE
        stage = posixpath.join(posixpath.dirname(upload['archive']),
                              'stage_' + upload['sha256'][:10] + '_' + uuid.uuid4().hex[:8])
        command = 'python3 -c %s %s %s %s' % tuple(shlex.quote(x) for x in
                   (EXTRACT_CODE, upload['archive'], stage, upload['sha256']))
        code, output = self.command(command, cancel, timeout=120)
        if code:
            raise RuntimeError(output)
        result = json.loads(output.strip().splitlines()[-1])
        result['identity'] = list(self.identity)
        return result

    def apply_command(self, stage, target, preview=True):
        source = self.path(stage['source'])
        target = self.path(target)
        if target in ('/', self.home) or target == source or source.startswith(target+'/') or target.startswith(source+'/'):
            raise ValueError('目标必须是独立工作区目录，不能是根目录、用户目录或暂存源码目录')
        if list(self.identity) != list(stage['identity']):
            raise ValueError('暂存包属于另一台SSH主机，请重新上传')
        return 'bash %s %s --dest %s' % (shlex.quote(source+'/tools/workspace_transfer/apply_workspace.sh'),
                                        '--dry-run' if preview else '--yes', shlex.quote(target))

    def backup(self, target, base, cancel=None):
        from remote_helpers import BACKUP_CODE
        command = 'python3 -c %s %s %s' % tuple(shlex.quote(x) for x in
                   (BACKUP_CODE, self.path(target), self.path(base)))
        code, output = self.command(command, cancel, timeout=300)
        if code:
            raise RuntimeError('源码备份未完成，安装未执行：\n'+output)
        return json.loads(output.strip().splitlines()[-1])

    def start_build(self, target, modules, jobs, base):
        target = self.path(target); base = self.path(base)
        parts = modules.split()
        if not parts or any(not re.fullmatch(r'[A-Za-z0-9_\-]+', p) for p in parts):
            raise ValueError('模块名只允许字母、数字、下划线和短横线，以空格分隔')
        if not 1 <= int(jobs) <= 64:
            raise ValueError('编译并行数应为1—64')
        job_dir = posixpath.join(base, 'build_' + time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
        code, output = self.command('mkdir -p -- '+shlex.quote(job_dir))
        if code:
            raise RuntimeError(output)
        status = job_dir+'/exit_code'; log = job_dir+'/build.log'
        script = ('#!/bin/bash\nset +e\n(\nset -e\nsource /opt/ros/noetic/setup.bash\n'
                  'cd '+shlex.quote(target)+'\n'
                  'if [ -f devel/setup.bash ]; then source devel/setup.bash; fi\n'
                  'bash ./build.sh -j '+str(int(jobs))+' '+ ' '.join(shlex.quote(p) for p in parts)+
                  '\n)\nresult=$?\nprintf "%s\\n" "$result" > '+shlex.quote(status)+
                  '\nexit "$result"\n')
        with self.client.open_sftp() as sftp:
            with sftp.open(job_dir+'/run.sh','w') as f:
                f.write(script)
        cmd = 'nohup bash %s > %s 2>&1 < /dev/null & echo $!' % (shlex.quote(job_dir+'/run.sh'),shlex.quote(log))
        code, output = self.command(cmd)
        if code:
            raise RuntimeError(output)
        return {'log':log,'status':status,'pid':output.strip().splitlines()[-1],
                'identity':list(self.identity),'target':target,'modules':parts}

    def follow_build(self, job, emit, cancel=None):
        if list(self.identity) != list(job['identity']):
            raise ValueError('编译任务属于另一台SSH主机')
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            offset = 0
            while True:
                check_cancel(cancel)
                with sftp.open(job['log'],'rb') as stream:
                    stream.seek(offset)
                    data = stream.read(256*1024)
                if data:
                    offset += len(data)
                    emit(data)
                try:
                    with sftp.open(job['status'],'r') as stream:
                        status = int(stream.read().strip())
                except FileNotFoundError:
                    status = None
                if status is not None:
                    with sftp.open(job['log'],'rb') as stream:
                        stream.seek(offset)
                        tail = stream.read()
                    if tail:
                        emit(tail)
                    return status
                time.sleep(.5)
