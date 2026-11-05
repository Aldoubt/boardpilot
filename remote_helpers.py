"""Small Python 3.8-compatible helper sent over an authenticated SSH connection."""
EXTRACT_CODE = r'''
import hashlib,json,os,posixpath,sys,tarfile
from pathlib import Path
archive,stage,expected=sys.argv[1:]
h=hashlib.sha256()
with open(archive,'rb') as f:
    for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
if h.hexdigest()!=expected:raise RuntimeError('archive checksum mismatch')
root=Path(stage)/'source';root.mkdir(parents=True,exist_ok=False)
with tarfile.open(archive,'r:gz') as tar:
    members=tar.getmembers()
    for m in members:
        name=m.name
        if not name or name.startswith('/') or '..' in name.split('/') or '\\' in name:
            raise RuntimeError('unsafe archive path: '+name)
        if not (m.isfile() or m.isdir() or m.issym()):raise RuntimeError('unsupported archive member: '+name)
        if m.issym():
            dest=posixpath.normpath(posixpath.join(posixpath.dirname(name),m.linkname))
            if m.linkname.startswith('/') or dest=='..' or dest.startswith('../'):
                raise RuntimeError('unsafe symlink: '+name)
    for m in members:
        target=(root/m.name).resolve()
        if root.resolve() not in target.parents:raise RuntimeError('archive escapes source directory')
        tar.extract(m,str(root))
if not (root/'build.sh').is_file() or not (root/'tools/workspace_transfer/apply_workspace.sh').is_file():
    raise RuntimeError('archive is not a deployable Sunray workspace')
manifest={}
if (root/'.boardpilot-manifest.json').is_file():
    manifest=json.loads((root/'.boardpilot-manifest.json').read_text())
print(json.dumps({'source':str(root),'archive':archive,'sha256':expected,'manifest':manifest}))
'''

INSTALLER_PREFLIGHT_CODE = r'''
import stat,sys
from pathlib import Path
source,target=sys.argv[1:]
installer=Path(source)/'tools/workspace_transfer/apply_workspace.sh'
marker='readonly SUNRAY_APPLY_SAFETY_VERSION=1'
try:
    if not stat.S_ISREG(installer.lstat().st_mode):
        raise ValueError('installer must be a regular file, not a symlink')
    if marker not in installer.read_text(encoding='utf-8').splitlines():
        raise ValueError('installer safety version 1 marker is missing')
except (OSError,UnicodeError,ValueError) as exc:
    raise SystemExit(
        'Installer safety check failed: %s. Update Sunray tools/workspace_transfer/'
        'apply_workspace.sh, then generate and upload a new package before retrying. '
        'Legacy installers may delete Git metadata. Target: %s' % (exc,target))
'''

BACKUP_CODE = r'''
import json,os,subprocess,sys,tarfile,tempfile,time,uuid
from pathlib import Path
root=Path(sys.argv[1]).resolve();base=Path(sys.argv[2]).resolve()
source=Path(sys.argv[3]).resolve() if len(sys.argv)>3 else None
if not root.exists():
    print(json.dumps({'backup':None,'reason':'target does not exist'}));raise SystemExit(0)
if root==base or root in base.parents:raise RuntimeError('backup must be outside target workspace')
if not (root/'build.sh').is_file():raise RuntimeError('existing target is not a Sunray workspace')
if source is not None and not source.is_dir():raise RuntimeError('source workspace does not exist')

git_env=os.environ.copy()
# Never let inherited Git variables redirect inspection or touch a caller's index.
for key in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR','GIT_PREFIX'):
    git_env.pop(key,None)
git_env['GIT_OPTIONAL_LOCKS']='0'

def git(directory,*args,required=True):
    result=subprocess.run(['git','-C',str(directory),*args],env=git_env,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode and required:raise RuntimeError(result.stderr.decode(errors='replace'))
    return result

# Keep the existing refusal to back up an unversioned target.
top=git(root,'rev-parse','--show-toplevel').stdout.decode(errors='surrogateescape').strip()
if Path(top).resolve()!=root:raise RuntimeError('target must be the root of its Git workspace')

excluded_roots=frozenset(('build','devel','install','logs','.catkin_tools','.venv'))
def excluded(name):
    parts=Path(name).parts
    return (not parts or '.git' in parts or parts[0] in excluded_roots or
            name=='localization/third_party_localization/openvins/ov_data' or
            name.startswith('localization/third_party_localization/openvins/ov_data/'))

def through_symlink(name):
    path=root/name
    return any(parent.is_symlink() for parent in path.parents
               if parent!=root and root in parent.parents)

# Read each available index independently. A submodule may have materialized
# source but no .git entry; filesystem collection below must still include it.
tracked=set();seen_repos=set()
def remember_tracked(directory):
    if directory in seen_repos:return
    seen_repos.add(directory)
    result=git(directory,'rev-parse','--show-toplevel',required=False)
    if result.returncode:return
    if Path(os.fsdecode(result.stdout).strip()).resolve()!=directory:return
    result=git(directory,'ls-files','--stage','-z',required=False)
    if result.returncode:return
    submodules=[]
    for entry in result.stdout.split(b'\0'):
        if not entry:continue
        fields,raw=entry.split(b'\t',1)
        name=(directory.relative_to(root)/os.fsdecode(raw)).as_posix()
        if excluded(name) or through_symlink(name):continue
        if fields.split()[0]==b'160000':
            path=root/name
            if path.is_dir() and not path.is_symlink():submodules.append(path)
        else:tracked.add(name)
    for path in submodules:remember_tracked(path)

remember_tracked(root)
commit_result=git(root,'rev-parse','HEAD',required=False)
status_result=git(root,'status','--short',required=False)
metadata={'commit':commit_result.stdout.decode(errors='replace').strip() if not commit_result.returncode else None,
          'status':status_result.stdout.decode(errors='replace')}
if status_result.returncode:metadata['status_error']=status_result.stderr.decode(errors='replace')

with tempfile.TemporaryDirectory(prefix='boardpilot-backup-git-') as scratch:
    contexts={}
    for rules in (root,source):
        if rules is None or rules in contexts:continue
        git_dir=Path(scratch)/str(len(contexts))
        result=subprocess.run(['git','--git-dir='+str(git_dir),'init','-q'],env=git_env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace'))
        contexts[rules]=git_dir

    def ignored_by(rules,paths):
        ignored=set()
        for start in range(0,len(paths),500):
            batch=paths[start:start+500]
            payload=b'\0'.join(os.fsencode(name) for name in batch)+b'\0'
            result=subprocess.run(['git','--git-dir='+str(contexts[rules]),'--work-tree='+str(rules),
                                   'check-ignore','--stdin','-v','-z','--no-index'],input=payload,
                                  env=git_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            if result.returncode not in (0,1):raise RuntimeError(result.stderr.decode(errors='replace'))
            fields=result.stdout.split(b'\0')
            for i in range(0,len(fields)-3,4):
                if fields[i+2] and not fields[i+2].startswith(b'!'):
                    ignored.add(os.fsdecode(fields[i+3]))
        return ignored

    def ignored_by_all(paths):
        # Target-ignored files may be deleted under incoming installer rules.
        # Preserve the union of files nonignored by either workspace.
        ignored=set(paths)
        for rules in contexts:ignored.intersection_update(ignored_by(rules,paths))
        return ignored

    names=set()
    for current,dirnames,filenames in os.walk(root,followlinks=False):
        directory=Path(current)
        if directory!=root and (directory/'.git').exists():remember_tracked(directory)
        directories=[];leaves=[]
        for filename in dirnames:
            path=directory/filename;name=path.relative_to(root).as_posix()
            if excluded(name):continue
            if path.is_symlink():leaves.append(name)
            else:directories.append(name)
        ignored_dirs=ignored_by_all(directories)
        dirnames[:]=[Path(name).name for name in directories if name not in ignored_dirs]
        for filename in filenames:
            name=(directory/filename).relative_to(root).as_posix()
            if not excluded(name):leaves.append(name)
        names.update(set(leaves)-ignored_by_all(leaves))
    names.update(tracked)

base.mkdir(parents=True,exist_ok=True)
archive=base/('source_backup_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.tar.gz')
part=Path(str(archive)+'.part');count=0
with tarfile.open(str(part),'w:gz',compresslevel=3,dereference=False) as tar:
    for name in sorted(names):
        if excluded(name) or through_symlink(name):continue
        p=root/name
        if p.is_file() or p.is_symlink():tar.add(str(p),arcname=name,recursive=False);count+=1
part.replace(archive)
metadata.update(backup=str(archive),files=count)
Path(str(archive)+'.json').write_text(json.dumps(metadata,indent=2))
print(json.dumps(metadata))
'''
