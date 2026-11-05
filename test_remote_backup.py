"""Execute remote helpers against disposable workspaces, never a live device."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from remote_helpers import BACKUP_CODE, INSTALLER_PREFLIGHT_CODE


class RemoteBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='boardpilot-backup-test-')
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'target'
        self.root.mkdir()
        self.git(self.root, 'init', '-q')
        self.git(self.root, 'config', 'user.name', 'Test')
        self.git(self.root, 'config', 'user.email', 'test@example.invalid')
        self.write(self.root, 'build.sh', '#!/bin/bash\n')
        self.write(self.root, '.gitignore', 'build/\nlogs/\n*.ignored\nsecret/\n')
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'initial')

    def tearDown(self):
        self.assertEqual(self.base.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.base.name.startswith('boardpilot-backup-test-'))
        self.tmp.cleanup()

    @staticmethod
    def write(root, name, content='source'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    @staticmethod
    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.STDOUT)

    def add_submodule(self, name='modules/child'):
        origin = self.base / ('origin-' + name.replace('/', '-'))
        origin.mkdir()
        self.git(origin, 'init', '-q')
        self.git(origin, 'config', 'user.name', 'Test')
        self.git(origin, 'config', 'user.email', 'test@example.invalid')
        self.write(origin, 'tracked.cpp')
        self.write(origin, '.gitignore', '*.ignored\n')
        self.git(origin, 'add', '.')
        self.git(origin, 'commit', '-qm', 'child')
        self.git(self.root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(origin), name)
        self.git(self.root, 'commit', '-qam', 'submodule')
        return self.root / name

    def backup(self, source=None, env=None):
        args = [sys.executable, '-c', BACKUP_CODE, str(self.root), str(self.base / 'backups')]
        if source is not None:
            args.append(str(source))
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return json.loads(result.stdout.decode())

    def members(self, metadata):
        with tarfile.open(metadata['backup']) as archive:
            return {member.name: member for member in archive.getmembers()}

    def git_metadata(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*')
                if '.git' in path.relative_to(self.root).parts and path.is_file()}

    def test_nested_untracked_materialized_submodule_and_git_metadata(self):
        child = self.add_submodule()
        self.write(child, 'nested/new.cpp')
        self.write(child, 'omit.ignored')
        # Force-tracked files remain recoverable even if an ignore rule matches.
        self.write(child, 'tracked.ignored')
        self.git(child, 'add', '-f', 'tracked.ignored')
        self.write(self.root, 'secret/forced.cpp')
        self.git(self.root, 'add', '-f', 'secret/forced.cpp')
        materialized = self.add_submodule('modules/materialized')
        (materialized / '.git').unlink()
        self.write(materialized, 'nested/local.cpp')
        self.write(materialized, 'omit.ignored')
        for name in ('build/cache', 'devel/setup', 'install/binary', 'logs/run.log',
                     '.catkin_tools/config', '.venv/bin/python',
                     'localization/third_party_localization/openvins/ov_data/data.bin',
                     'nested/.git/config'):
            self.write(self.root, name)
        before = self.git_metadata()
        metadata = self.backup()
        names = self.members(metadata)
        for name in ('build.sh', 'modules/child/tracked.cpp', 'modules/child/nested/new.cpp',
                     'modules/child/tracked.ignored', 'modules/materialized/tracked.cpp',
                     'modules/materialized/nested/local.cpp', 'secret/forced.cpp'):
            self.assertIn(name, names)
        self.assertNotIn('modules/child/omit.ignored', names)
        self.assertNotIn('modules/materialized/omit.ignored', names)
        self.assertFalse(any('.git' in Path(name).parts for name in names))
        for prefix in ('build/', 'devel/', 'install/', 'logs/', '.catkin_tools/', '.venv/',
                       'localization/third_party_localization/openvins/ov_data/'):
            self.assertFalse(any(name.startswith(prefix) for name in names), prefix)
        self.assertEqual(self.git_metadata(), before)
        self.assertEqual(metadata['files'], len(names))

    def test_incoming_ignore_rules_preserve_target_only_ignored_files(self):
        self.write(self.root, 'local.ignored')
        self.write(self.root, 'secret/not-tracked.cpp')
        self.write(self.root, 'shared.skip')
        with (self.root / '.gitignore').open('a') as stream:
            stream.write('*.skip\n')
        source = self.base / 'source'
        source.mkdir()
        self.write(source, '.gitignore', '*.skip\n')
        names = self.members(self.backup(source))
        self.assertIn('local.ignored', names)
        self.assertIn('secret/not-tracked.cpp', names)
        self.assertNotIn('shared.skip', names)

    def test_symlink_directories_are_archived_without_following(self):
        outside = self.base / 'outside'
        self.write(outside, 'private.txt')
        link = self.root / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('directory symlinks unavailable: %s' % exc)
        before = self.git_metadata()
        names = self.members(self.backup())
        self.assertTrue(names['linked'].issym())
        self.assertNotIn('linked/private.txt', names)
        self.assertEqual(self.git_metadata(), before)

    def test_inherited_index_cannot_modify_real_git_metadata(self):
        before = self.git_metadata()
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = str(self.root / '.git/index')
        env['GIT_DIR'] = str(self.root / '.git')
        env['GIT_WORK_TREE'] = str(self.root)
        self.assertIn('build.sh', self.members(self.backup(env=env)))
        self.assertEqual(self.git_metadata(), before)


class InstallerPreflightTests(unittest.TestCase):
    def test_marker_required_and_no_script_execution(self):
        with tempfile.TemporaryDirectory(prefix='boardpilot-installer-test-') as temp:
            base = Path(temp)
            installer = base / 'source/tools/workspace_transfer/apply_workspace.sh'
            installer.parent.mkdir(parents=True)
            for content, accepted in (
                    ('#!/bin/bash\nreadonly SUNRAY_APPLY_SAFETY_VERSION=1\n', True),
                    ('#!/bin/bash\r\nreadonly SUNRAY_APPLY_SAFETY_VERSION=1\r\n', True),
                    ('#!/bin/bash\nexit 0\n', False),
                    ('# readonly SUNRAY_APPLY_SAFETY_VERSION=1\n', False),
                    ('readonly SUNRAY_APPLY_SAFETY_VERSION=10\n', False)):
                installer.write_bytes(content.encode())
                result = subprocess.run([sys.executable, '-c', INSTALLER_PREFLIGHT_CODE,
                                         str(base / 'source'), str(base / 'target')],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode == 0, accepted, result.stderr.decode(errors='replace'))
                if not accepted:
                    self.assertIn(b'generate and upload a new package', result.stderr)
                self.assertFalse((base / 'target').exists())

    def test_symlink_installer_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='boardpilot-installer-test-') as temp:
            base = Path(temp)
            source = base / 'source'
            installer = source / 'tools/workspace_transfer/apply_workspace.sh'
            installer.parent.mkdir(parents=True)
            actual = base / 'actual.sh'
            actual.write_text('readonly SUNRAY_APPLY_SAFETY_VERSION=1\n')
            try:
                installer.symlink_to(actual)
            except (OSError, NotImplementedError) as exc:
                self.skipTest('file symlinks unavailable: %s' % exc)
            result = subprocess.run([sys.executable, '-c', INSTALLER_PREFLIGHT_CODE,
                                     str(source), str(base / 'target')],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'regular file', result.stderr)


if __name__ == '__main__':
    unittest.main()
