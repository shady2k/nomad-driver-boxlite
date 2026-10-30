"""Trusted ACP-adapter paths. No workload-selected host path or service manager."""
import os,platform,pwd,stat
from pathlib import Path

def default_root(system=None,home=None,uid=None):
 system=system or platform.system();home=Path(home or pwd.getpwuid(os.getuid()).pw_dir);uid=os.getuid() if uid is None else uid
 if system=='Darwin':return home/'Library/Application Support/BoxLiteNomad'
 if system=='Linux':return Path('/var/lib/boxlite-nomad') if uid==0 else home/'.local/state/boxlite-nomad'
 raise ValueError('Supported node platforms: Linux and Darwin')
def root(module_file):
 # Deployment may use immutable releases/current symlinks. Only an owned,
 # protected node marker identifies the host root; payload cannot choose it.
 start=Path(module_file).resolve().parent
 for candidate in [start,*list(start.parents)[:5]]:
  p=candidate/'node.json'
  try:s=p.lstat()
  except FileNotFoundError:continue
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600:raise ValueError('Unsafe operator node marker')
  return candidate
 return default_root()
def executable_path(system=None):
 system=system or platform.system()
 if system=='Darwin':return '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
 if system=='Linux':return '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
 raise ValueError('Unsupported host platform')


def trusted_runtime(operator, system=None):
 """Optional operator-selected, hash-verified runtime assets; never caller env."""
 import hashlib,re
 selected=operator.get('runtime_assets')
 if selected is None:return None
 if not isinstance(selected,dict) or set(selected)!={'directory','files'}:
  raise ValueError('Invalid runtime assets selection')
 directory=Path(selected['directory'])
 files=selected['files']
 if not directory.is_absolute() or directory.resolve(strict=True)!=directory:
  raise ValueError('Runtime assets require a canonical absolute path')
 uid=os.getuid()
 for parent in [directory,*directory.parents]:
  st=parent.lstat()
  sticky_root=st.st_uid==0 and bool(st.st_mode & stat.S_ISVTX)
  if not stat.S_ISDIR(st.st_mode) or st.st_uid not in {0,uid} or (st.st_mode & 0o022 and not sticky_root):
   raise ValueError('Unsafe runtime assets directory')
 if directory.stat().st_uid!=uid:raise ValueError('Runtime assets owner mismatch')
 required={'boxlite-shim','boxlite-guest'}
 if (system or platform.system())=='Linux':required|={'bwrap','libkrunfw.so.5'}
 if not isinstance(files,dict) or not required.issubset(files):
  raise ValueError('Incomplete runtime assets manifest')
 for name,digest in files.items():
  if not isinstance(name,str) or name in {'.','..'} or not re.fullmatch(r'[A-Za-z0-9._-]+',name) or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
   raise ValueError('Invalid runtime assets manifest')
 dfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  if set(os.listdir(dfd))!=set(files):raise ValueError('Unlisted runtime assets')
  for name,expected in files.items():
   fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=dfd)
   with os.fdopen(fd,'rb') as f:
    st=os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=uid or st.st_mode & 0o022:
     raise ValueError('Unsafe runtime asset: '+name)
    if name in required-{'libkrunfw.so.5'} and not st.st_mode & stat.S_IXUSR:
     raise ValueError('Runtime executable missing mode: '+name)
    h=hashlib.sha256()
    for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    if h.hexdigest()!=expected:raise ValueError('Runtime asset hash mismatch: '+name)
 finally:os.close(dfd)
 return str(directory)
