#!/usr/bin/env python3
"""Manual operator cleanup. Preview by default; requires explicit stopped VM IDs.
No automatic GC. Never delete a native box before its driver reservation is released.
"""
import argparse,contextlib,fcntl,hashlib,json,os,re,stat,subprocess,urllib.request,urllib.error,urllib.parse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent.parent/"examples/prime"))
import node_paths

def protected(path):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd) as f:
  s=os.fstat(f.fileno())
  if s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600 or not stat.S_ISREG(s.st_mode):raise ValueError('Unsafe operator file')
  return json.load(f)
def require_released(records,vm):
 matches=[r for r in records if r.get('VMID')==vm]
 if not matches:raise ValueError('unknown_driver_ownership')
 for d in matches:
  if d.get('Key')!=hashlib.sha256((d['AllocID']+'\0'+d['TaskName']).encode()).hexdigest():raise ValueError('driver_identity_mismatch')
  if d.get('Destroyed') is not True:raise ValueError('driver_reservation_not_released; use Nomad lifecycle or separately audited reconciliation')
 return matches

def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=node_paths.root(__file__));p.add_argument('--vm-id',action='append',default=[]);p.add_argument('--apply',action='store_true');a=p.parse_args(argv)
 if a.apply and not a.vm_id:raise ValueError('Apply requires explicitly selected VM IDs from preview')
 root=a.root.resolve();state=root/'driver-state';runtime=root/'runtime/boxes';node=protected(root/'node.json');operator=protected(root/'operator.json');u=urllib.parse.urlsplit(operator['rest_endpoint'])
 if u.scheme!='http' or u.hostname!='127.0.0.1' or not u.port or u.username or u.password or u.path or u.query or u.fragment:raise ValueError('Loopback operator REST origin required')
 key=subprocess.run(node['rest_key_command'],capture_output=True,check=True,timeout=10).stdout.decode().strip()
 def call(path,method='GET'):
  try:
   with urllib.request.urlopen(urllib.request.Request(operator['rest_endpoint']+path,method=method,headers={'Authorization':'Bearer '+key}),timeout=10) as f:raw=f.read(1048577);return f.status,json.loads(raw) if raw else None
  except urllib.error.HTTPError as e:return e.code,None
 def records():return [protected(f) for f in state.glob('*.json')]
 code,inventory=call('/v1/boxes')
 if code!=200 or inventory.get('next_cursor'):raise ValueError('Complete native inventory unavailable')
 report=[]
 for box in inventory['boxes']:
  vm=box['box_id']
  if a.vm_id and vm not in a.vm_id:continue
  row={'vm_id':vm,'native_status':box['status'],'action':'skipped'}
  try:
   if not re.fullmatch('[A-Za-z0-9_-]{1,128}',vm):raise ValueError('Native VM ID invalid')
   if box['status']!='stopped' or box.get('pid'):raise ValueError('not_stopped')
   matches=require_released(records(),vm)
   if not a.apply:row.update(action='preview_eligible',disk_deletion_requires_explicit_apply=True);report.append(row);continue
   with contextlib.ExitStack() as stack:
    for path in [state/(r['Key']+'.lock') for r in sorted(matches,key=lambda r:r['Key'])]+[state/'node-budget.lock']:
     fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW);stack.callback(os.close,fd);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);stack.callback(fcntl.flock,fd,fcntl.LOCK_UN)
    # Re-read inside the same locks used by driver admission. Do not mutate ledger.
    require_released(records(),vm);code,current=call('/v1/boxes/'+vm)
    if code!=200 or current['status']!='stopped' or current.get('pid') or current['name']!=box['name']:raise ValueError('native_identity_changed')
    directory=runtime/vm
    if directory.is_symlink() or directory.resolve().parent!=runtime.resolve():raise ValueError('unsafe_box_path')
    files=[]
    for base,dirs,names in os.walk(directory/'disks',followlinks=False):
     for n in dirs+names:
      f=Path(base)/n;s=f.lstat()
      if stat.S_ISLNK(s.st_mode) or not f.resolve().is_relative_to(directory.resolve()):raise ValueError('disk_path_escape')
      if stat.S_ISREG(s.st_mode):
       if s.st_uid!=os.getuid() or s.st_nlink!=1:raise ValueError('unowned_or_shared_disk')
       files.append(str(f))
      elif not stat.S_ISDIR(s.st_mode):raise ValueError('special_disk_file')
    if not files:raise ValueError('no_owned_disk_files')
    opened=subprocess.run(['/usr/sbin/lsof','-nP','--']+files,capture_output=True,text=True,timeout=8)
    if opened.returncode!=1 or opened.stdout.strip() or opened.stderr.strip():raise ValueError('disk_openers_unknown_or_active')
    code,_=call('/v1/boxes/'+vm+'?force=false','DELETE')
    if code not in (200,204):raise ValueError('native_delete_not_confirmed')
    code,_=call('/v1/boxes/'+vm)
    if code!=404 or os.path.lexists(directory):raise ValueError('delete_readback_uncertain')
    row['action']='deleted_verified'
  except Exception as e:row['reason']=str(e) if isinstance(e,ValueError) else type(e).__name__
  report.append(row)
 return {'manual_cleanup':True,'apply':a.apply,'automatic_gc':False,'results':report}
if __name__=='__main__':
 try:print(json.dumps(main()))
 except Exception as e:print(json.dumps({'error':type(e).__name__,'no_automatic_retry':True}));raise SystemExit(2)
