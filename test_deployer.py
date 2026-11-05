import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from deployer_core import (Cancelled,SSHSession,inspect_workspace,package_workspace,
                           validate_connection)
from remote_helpers import EXTRACT_CODE


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='sunray-deployer-test-')
        self.base=Path(self.tmp.name).resolve();self.repo=self.base/'repo';self.repo.mkdir()
        self.git('init','-q');self.git('config','user.email','test@example.invalid');self.git('config','user.name','Test')
        (self.repo/'build.sh').write_bytes(b'#!/bin/bash\r\necho old\r\n')
        (self.repo/'.gitignore').write_text('build/\ndevel/\nignored.txt\n')
        (self.repo/'tools/workspace_transfer').mkdir(parents=True)
        (self.repo/'tools/workspace_transfer/apply_workspace.sh').write_text('#!/bin/bash\nexit 0\n')
        self.git('add','.');self.git('update-index','--chmod=+x','build.sh');self.git('commit','-qm','initial')

    def tearDown(self):
        # Verify the resolved recursive cleanup target is our own temp directory.
        self.assertEqual(self.base.parent,Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.base.name.startswith('sunray-deployer-test-'))
        self.tmp.cleanup()

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.STDOUT)

    def package(self):return package_workspace(self.repo,self.base/'packages')

    def test_worktree_modifications_ignore_rules_modes_and_manifest(self):
        (self.repo/'build').mkdir();(self.repo/'build/cache').write_text('omit')
        (self.repo/'ignored.txt').write_text('omit');(self.repo/'new.py').write_text('new')
        (self.repo/'build.sh').write_bytes(b'#!/bin/bash\r\necho modified\r\n')
        receipt=self.package()
        with tarfile.open(receipt['path']) as tar:
            names=tar.getnames();self.assertIn('new.py',names);self.assertNotIn('build/cache',names)
            self.assertNotIn('ignored.txt',names);self.assertFalse(any('.git/' in x for x in names))
            self.assertEqual(tar.extractfile('build.sh').read(),b'#!/bin/bash\necho modified\n')
            self.assertEqual(tar.getmember('build.sh').mode,0o755)
            manifest=json.load(tar.extractfile('.boardpilot-manifest.json'))
            self.assertEqual(manifest['commit'],receipt['commit']);self.assertTrue(manifest['status'])
        self.assertEqual(hashlib.sha256(Path(receipt['path']).read_bytes()).hexdigest(),receipt['sha256'])

    def test_output_inside_source_is_rejected(self):
        with self.assertRaises(ValueError):package_workspace(self.repo,self.repo/'artifacts')

    def test_git_worktree_supported(self):
        # inspect_workspace accepts .git files used by Git worktrees, unlike the legacy packer.
        worktree=self.base/'worktree';self.git('worktree','add','--detach',str(worktree),'HEAD')
        self.assertTrue((worktree/'.git').is_file());self.assertTrue(inspect_workspace(worktree)['files'])

    def test_cancel_does_not_publish_partial_package(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):package_workspace(self.repo,self.base/'packages',cancel=cancel)
        self.assertEqual(list((self.base/'packages').glob('*.tar.gz')),[])
        self.assertEqual(list((self.base/'packages').glob('*.part')),[])

    def extract(self,archive,stage,sha=None):
        return subprocess.run([sys.executable,'-c',EXTRACT_CODE,str(archive),str(stage),sha or hashlib.sha256(Path(archive).read_bytes()).hexdigest()],capture_output=True,text=True)

    def test_remote_helper_extracts_source_without_touching_target(self):
        receipt=self.package();stage=self.base/'stage';result=self.extract(receipt['path'],stage)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((stage/'source/build.sh').is_file())
        self.assertTrue((self.repo/'build.sh').is_file())

    def test_remote_helper_rejects_traversal_and_checksum_mismatch(self):
        archive=self.base/'bad.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            member=tarfile.TarInfo('../outside');member.size=3;tar.addfile(member,io.BytesIO(b'bad'))
        result=self.extract(archive,self.base/'badstage');self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.base/'outside').exists())
        receipt=self.package();result=self.extract(receipt['path'],self.base/'badsha','0'*64)
        self.assertNotEqual(result.returncode,0);self.assertFalse((self.base/'badsha').exists())

    def test_remote_helper_rejects_external_symlink(self):
        archive=self.base/'badlink.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            m=tarfile.TarInfo('a');m.type=tarfile.SYMTYPE;m.linkname='../escape';tar.addfile(m)
        self.assertNotEqual(self.extract(archive,self.base/'linkstage').returncode,0)

    def test_target_validation_and_shell_quoting(self):
        class Transport:
            def is_active(self):return True
        class Client:
            def get_transport(self):return Transport()
        session=SSHSession(self.base);session.client=Client();session.home='/home/user';session.identity=('host',22,'user')
        stage={'source':'/home/user/incoming/source','identity':['host',22,'user']}
        with self.assertRaises(ValueError):session.apply_command(stage,'~',True)
        with self.assertRaises(ValueError):session.apply_command(stage,'/home/user/incoming',True)
        target='/home/user/repo with space; echo unsafe'
        command=session.apply_command(stage,target,True)
        self.assertEqual(shlex.split(command)[-1],target)
        with self.assertRaises(ValueError):validate_connection('host;echo bad',22,'user')
        with self.assertRaises(ValueError):validate_connection('host',0,'user')


class FakeFile(io.BytesIO):
    def __init__(self,store,path,mode):
        self.store,self.path,self.mode=store,path,mode
        super().__init__(store.get(path,b''))
        if 'a' in mode:self.seek(0,2)
        if 'w' in mode:self.seek(0);self.truncate()
    def set_pipelined(self,_):pass
    def close(self):
        if not self.closed and any(x in self.mode for x in ('a','w')):self.store[self.path]=self.getvalue()
        super().close()


class FakeSFTP:
    def __init__(self):self.files={};self.dirs={'/','/home','/home/user'};self.renames=[]
    def get_channel(self):return self
    def settimeout(self,_):pass
    def stat(self,path):
        class Attr:pass
        a=Attr()
        if path in self.dirs:a.st_size=0;a.st_mode=stat.S_IFDIR|0o755;return a
        if path in self.files:a.st_size=len(self.files[path]);a.st_mode=stat.S_IFREG|0o644;return a
        raise FileNotFoundError(path)
    def mkdir(self,path):self.dirs.add(path)
    def open(self,path,mode):
        if path not in self.files and mode=='rb':raise FileNotFoundError(path)
        return FakeFile(self.files,path,mode)
    def rename(self,src,dst):self.files[dst]=self.files.pop(src);self.renames.append((src,dst))
    def remove(self,path):del self.files[path]
    def close(self):pass


class UploadTests(unittest.TestCase):
    def test_interrupted_upload_resumes_and_checks_before_rename(self):
        import paramiko
        with tempfile.TemporaryDirectory(prefix='sunray-upload-test-') as tmp:
            path=Path(tmp)/'source.tar.gz';payload=os.urandom(700000);path.write_bytes(payload)
            fake=FakeSFTP();cancel=threading.Event()
            class Session(SSHSession):
                def require(self):pass
                def command(self,command,*args,**kwargs):
                    remote=shlex.split(command)[-1]
                    return 0,hashlib.sha256(fake.files[remote]).hexdigest()+'  '+remote
            class Client:
                def get_transport(self):return object()
            session=Session(tmp);session.home='/home/user';session.client=Client()
            with patch.object(paramiko.SFTPClient,'from_transport',return_value=fake):
                def stop_after_chunk(done,total):
                    if done>=256*1024:cancel.set()
                with self.assertRaises(Cancelled):session.upload(path,'~/incoming',stop_after_chunk,cancel)
                part=next(iter(fake.files));self.assertTrue(part.endswith('.part'))
                self.assertEqual(fake.files[part],payload[:len(fake.files[part])]);self.assertFalse(fake.renames)
                session.upload(path,'~/incoming')
                self.assertEqual(fake.files['/home/user/incoming/source.tar.gz'],payload)
                self.assertEqual(len(fake.renames),1)
            self.assertEqual(Path(tmp).resolve().parent,Path(tempfile.gettempdir()).resolve())


if __name__=='__main__':unittest.main()
