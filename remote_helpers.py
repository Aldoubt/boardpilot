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

BACKUP_CODE = r'''
import json,os,subprocess,sys,tarfile,time,uuid
from pathlib import Path
root=Path(sys.argv[1]).resolve();base=Path(sys.argv[2]).resolve()
if not root.exists():
    print(json.dumps({'backup':None,'reason':'target does not exist'}));raise SystemExit(0)
if root==base or root in base.parents:raise RuntimeError('backup must be outside target workspace')
if not (root/'build.sh').is_file():raise RuntimeError('existing target is not a Sunray workspace')
def git(*args):
    p=subprocess.run(['git','-C',str(root),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace'))
    return p.stdout
# Refuse an unversioned existing target rather than silently missing its modifications.
git('rev-parse','--show-toplevel')
names=set(git('ls-files','-z','--recurse-submodules').split(b'\0'))
names.update(git('ls-files','-z','--others','--exclude-standard').split(b'\0'))
base.mkdir(parents=True,exist_ok=True)
archive=base/('source_backup_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.tar.gz')
part=Path(str(archive)+'.part');count=0
with tarfile.open(str(part),'w:gz',compresslevel=3,dereference=False) as tar:
    for raw in sorted(names):
        if not raw:continue
        name=os.fsdecode(raw);parts=Path(name).parts
        if '.git' in parts or parts[0] in ('build','devel','install','logs','.catkin_tools','.venv'):continue
        if name.startswith('localization/third_party_localization/openvins/ov_data/'):continue
        p=root/name
        if p.is_file() or p.is_symlink():tar.add(str(p),arcname=name,recursive=False);count+=1
part.replace(archive)
metadata={'backup':str(archive),'files':count,'commit':git('rev-parse','HEAD').decode().strip(),'status':git('status','--short').decode(errors='replace')}
Path(str(archive)+'.json').write_text(json.dumps(metadata,indent=2))
print(json.dumps(metadata))
'''
