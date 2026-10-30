"""Observed idle/grace/holds. No lifetime deadline, scheduler, or automatic stop."""
import hashlib,json,math,os,time,uuid,fcntl
from pathlib import Path

def root():return Path.home()/'.local/state/nomad-boxlite-prime'
def protected(path):
 import durable_export
 return durable_export.read_json(root(),path)[0]
def write(path,d):
 import durable_export,tempfile
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 if path.parent.is_symlink() or path.parent.stat().st_uid!=os.getuid() or path.parent.stat().st_mode&0o077:raise ValueError('Unsafe lifecycle directory')
 fd,tmp=tempfile.mkstemp(prefix='.lifecycle-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as f:f.write(durable_export.encoded(d));f.flush();os.fsync(f.fileno())
  os.replace(tmp,path);durable_export.fsync_dir(path.parent)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def load_policy():
 p=Path.home()/'.config/nomad-boxlite-prime/lifecycle.json'
 if not p.exists():return {'auto_close_enabled':False,'idle_grace_seconds':None}
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd) as f:
  import stat
  st=os.fstat(f.fileno())
  if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):raise ValueError('Unsafe lifecycle config')
  d=json.load(f)
 if set(d)!={'auto_close_enabled','idle_grace_seconds'} or d['auto_close_enabled'] is not False:raise ValueError('Automatic close is not enabled by this implementation')
 n=d['idle_grace_seconds']
 if n is not None and (type(n) not in (int,float) or not math.isfinite(n) or n<0):raise ValueError('Invalid configurable idle grace seconds')
 return d

def hold(job,sid,seconds=None,release=False):
 sid=str(uuid.UUID(sid));p=root()/'holds'/sid/'hold.json'
 if not release and (type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<=0):raise ValueError('Positive finite hold seconds required')
 d={'job':job,'session_id':sid,'hold_until':None if release else time.time()+seconds,'changed_at':time.time(),'auto_close_enabled':False};write(p,d);return d

def decorate(job,info,out,now=None):
 now=time.time() if now is None else now;result=dict(out);result['auto_close_enabled']=False;result['auto_close_eligible']=False
 try:
  config=load_policy();sid=str(uuid.UUID(info.get('session_id')));directory=root()/'idle-observations'/sid;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
  if directory.is_symlink() or directory.stat().st_mode&0o077:raise ValueError('Unsafe observation directory')
  fd=os.open(directory/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:
   fcntl.flock(fd,fcntl.LOCK_EX);p=directory/'state.json';old=protected(p) if p.exists() else {};rows=info.get('requests',[])
   fingerprint=hashlib.sha256(json.dumps([(r.get('request_id'),r.get('state')) for r in rows],sort_keys=True).encode()).hexdigest()
   ready=out['state'] in ('ready-exported','ready-unexported') and info.get('state')=='ready' and not info.get('active_request_id')
   since=old.get('ready_observed_since') if ready and old.get('fingerprint')==fingerprint and old.get('job')==job else None
   if ready and since is None:since=now
   write(p,{'job':job,'session_id':sid,'fingerprint':fingerprint,'ready_observed_since':since,'observed_at':now})
  finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
  hp=root()/'holds'/sid/'hold.json';h=protected(hp) if hp.exists() else {};until=h.get('hold_until') if h.get('job')==job else None;held=until is not None and until>now
  grace=config['idle_grace_seconds'];elapsed=max(0,now-since) if since is not None else None
  result.update({'idle_observed_since':since,'idle_elapsed_seconds':elapsed,'idle_grace_seconds':grace,'hold_active':held,'hold_until':until,'idle_policy_state':'held' if held else 'not_idle' if since is None else 'unexported' if out['state']!='ready-exported' else 'grace_unconfigured' if grace is None else 'grace_elapsed' if elapsed>=grace else 'within_grace','observation_is_not_progress':True})
 except (OSError,ValueError,KeyError,TypeError):result.update({'idle_policy_state':'unknown','idle_policy_error':'configuration_or_observation_unavailable'})
 return result
