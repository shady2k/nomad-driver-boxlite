"""Verified declared-result commits on existing coordinator storage, outside VM.
Not an S3 replica, whole-filesystem backup, permission to close, or agent checkpoint.
"""
import hashlib,json,os,re,stat,time,uuid
from pathlib import Path,PurePosixPath
TERMINAL={'completed','cancelled','failed_no_retry','uncertain_no_retry'}
SECRET_PATH=re.compile(r'(^|/)(\.env[^/]*|\.ssh|\.aws|\.kube|credentials|id_rsa|id_ed25519)(/|$)|\.(pem|key)$')
def base():return Path.home()/'.local/state/nomad-boxlite-prime'
def uid(value):return str(uuid.UUID(value))
def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def relative(path):
 if not isinstance(path,str):raise ValueError('Declared path must be text')
 p=PurePosixPath(path)
 if not isinstance(path,str) or p.is_absolute() or not p.parts or any(x in ('','..','.') for x in path.split('/')) or '\\' in path:raise ValueError('Unsafe declared path')
 if SECRET_PATH.search(path):raise ValueError('Secret path is not an export declaration')
 return path

def protected_open(root,path,sync=False):
 root=Path(root);path=Path(path)
 try:parts=path.absolute().relative_to(root.absolute()).parts
 except ValueError:raise ValueError('Export path outside existing coordinator store') from None
 if not parts or any(x in ('.','..') for x in parts):raise ValueError('Unsafe export path')
 fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  st=os.fstat(fd)
  if st.st_uid!=os.getuid() or st.st_mode&0o022:raise ValueError('Unsafe export store')
  for part in parts[:-1]:
   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   st=os.fstat(nxt)
   if st.st_uid!=os.getuid() or st.st_mode&0o022:os.close(nxt);raise ValueError('Unsafe export directory')
   if sync:os.fsync(fd)
   os.close(fd);fd=nxt
  out=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd);st=os.fstat(out)
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:os.close(out);raise ValueError('Expected own protected regular export file')
  if sync:os.fsync(fd)
  return out
 finally:os.close(fd)
def read_json(root,path):
 with os.fdopen(protected_open(root,path),'rb') as f:raw=f.read(1048577)
 if len(raw)>1048576:raise ValueError('Export metadata quota exceeded')
 return json.loads(raw),digest(raw)
def stat_identity(st):return {'size':st.st_size,'mtime_ns':st.st_mtime_ns,'inode':st.st_ino}
def file_proof(root,path):
 with os.fdopen(protected_open(root,path,True),'rb') as f:
  before=os.fstat(f.fileno());h=hashlib.sha256()
  for block in iter(lambda:f.read(1048576),b''):h.update(block)
  os.fsync(f.fileno());after=os.fstat(f.fileno())
 if stat_identity(before)!=stat_identity(after):raise ValueError('Export changed during verification')
 return {'sha256':h.hexdigest(),**stat_identity(after)}
def ensure_dir(path):
 path.mkdir(mode=0o700,exist_ok=True)
 st=path.lstat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:raise ValueError('Unsafe commit directory')
def fsync_dir(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:os.fsync(fd)
 finally:os.close(fd)
def tracked(job):
 if not re.fullmatch(r'worker-[0-9a-f-]{36}',job):return False
 try:d,_=read_json(base(),base()/'receipts'/(job[7:]+'.json'))
 except (OSError,ValueError):return False
 return d.get('state')=='dispatched' and d.get('response',{}).get('DispatchedJobID')==job and d.get('response',{}).get('StateContractVersion')==1

def acknowledge(job,info,request_id,manifest_path,declared_paths,all_outputs_declared):
 sid=uid(info.get('session_id'));rid=uid(request_id)
 if info.get('state')!='ready' or info.get('active_request_id') or any(x.get('state') in ('accepted','running') for x in info.get('requests',[])):raise ValueError('Export acknowledgement requires observed ready/no active request')
 rec=next((x for x in info.get('requests',[]) if x.get('request_id')==rid),None)
 if not rec or rec.get('state') not in TERMINAL:raise ValueError('Terminal request identity required')
 if all_outputs_declared is not True:raise ValueError('Explicit declaration of all result outputs required; a download alone is not acknowledgement')
 paths=[relative(x) for x in declared_paths]
 if not paths or len(paths)>4096 or len(set(paths))!=len(paths):raise ValueError('Explicit unique declared output paths required')
 result_path=base()/'results'/(rid+'.json');result,result_sha=read_json(base(),result_path)
 if result.get('job')!=job or result.get('result',{}).get('request_id')!=rid or result.get('result',{}).get('session_id')!=sid:raise ValueError('Saved result binding mismatch')
 if result['result'].get('state') not in TERMINAL or result['result'].get('state')!=rec['state']:raise ValueError('Saved result is not the observed terminal result')
 full_result=result['result'].get('result_file')
 if not full_result or full_result not in paths:raise ValueError('Include the complete guest result file, not just its truncated status summary')
 manifest_path=Path(manifest_path).absolute();manifest,manifest_sha=read_json(base()/'artifacts',manifest_path)
 if manifest.get('job')!=job or manifest.get('session_id')!=sid:raise ValueError('Artifact manifest binding mismatch')
 items=manifest.get('artifacts',[])
 if sorted(x.get('path','') for x in items)!=sorted(paths):raise ValueError('Manifest must exactly cover all explicitly declared result outputs')
 proofs=[]
 for item in items:
  path=relative(item['path']);local=Path(item['local_path']).absolute();expected=manifest_path.parent/'files'/path
  if local!=expected:raise ValueError('Artifact local path mismatch')
  proof=file_proof(base()/'artifacts',local)
  if proof['sha256']!=item['sha256'] or proof['size']!=item.get('bytes'):raise ValueError('Artifact hash/size mismatch')
  proofs.append({'path':path,'local_path':str(local),**proof})
 doc={'version':1,'job':job,'session_id':sid,'request_id':rid,'scope':'all-coordinator-declared-result-outputs','all_outputs_declared':True,'storage_kind':'coordinator-host','replicated':False,'agent_resume_checkpoint':False,'result_path':str(result_path),'result_sha256':result_sha,'source_manifest':str(manifest_path),'source_manifest_sha256':manifest_sha,'files':proofs}
 raw=encoded(doc);sha=digest(raw);exports=base()/'exports';ensure_dir(exports);parent=exports/sid;ensure_dir(parent);dest=parent/rid
 if dest.exists():
  old=committed(job,sid,rid)
  if old['manifest_sha256']!=sha:raise ValueError('Existing committed export differs; never overwrite')
  return {**old,'idempotent':True}
 temp=parent/('.pending-'+rid+'-'+uuid.uuid4().hex);ensure_dir(temp)
 for name,data in [('manifest.json',raw),('COMMITTED',encoded({'manifest_sha256':sha,'committed_at':time.time()}))]:
  fd=os.open(temp/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 fsync_dir(temp)
 try:os.rename(temp,dest)
 except OSError:
  if not dest.exists():raise
  old=committed(job,sid,rid)
  if old['manifest_sha256']!=sha:raise ValueError('Concurrent export commit differs')
  for name in ['manifest.json','COMMITTED']:(temp/name).unlink()
  temp.rmdir()
  return {**old,'idempotent':True}
 fsync_dir(parent);fsync_dir(exports)
 return committed(job,sid,rid)
def committed(job,sid,rid):
 root=base()/'exports'/uid(sid)/uid(rid);commit,_=read_json(base(),root/'COMMITTED');doc,sha=read_json(base(),root/'manifest.json')
 if commit.get('manifest_sha256')!=sha or doc.get('job')!=job or doc.get('session_id')!=sid or doc.get('request_id')!=rid:raise ValueError('Export commit binding mismatch')
 for key,path in [('result_sha256',doc['result_path']),('source_manifest_sha256',doc['source_manifest'])]:
  _,actual=read_json(base(),path)
  if actual!=doc[key]:raise ValueError('Export evidence changed after commit')
 for item in doc['files']:
  fd=protected_open(base()/'artifacts',item['local_path'])
  try:seen=stat_identity(os.fstat(fd))
  finally:os.close(fd)
  if seen!={k:item[k] for k in seen}:raise ValueError('Exported file changed after commit')
 return {'phase':'export_committed','job':job,'session_id':sid,'request_id':rid,'manifest_sha256':sha,'commit_path':str(root/'COMMITTED'),'storage_kind':'coordinator-host','durability':'verified-and-fsynced-on-existing-coordinator-disk','replicated':False,'agent_resume_checkpoint':False,'auto_close_enabled':False,'verification_level':'content-hashes-at-commit-and-current-file-stat','committed_at':commit['committed_at']}
def classify_base(job,info):
 out={'state':'unknown','auto_close_enabled':False,'storage_kind':'coordinator-host','replicated':False,'checkpoint_resume_verified':False}
 if not isinstance(info,dict):return {**out,'reason_code':'guest_observation_unavailable'}
 rows=info.get('requests',[])
 if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):return {**out,'reason_code':'request_inventory_invalid'}
 active=info.get('active_request_id') or any(x.get('state') in ('accepted','running') for x in rows)
 if any(q.get('state')=='pending' for q in (info.get('questions') or []) if isinstance(q,dict)):return {**out,'state':'awaiting-input','busy':True,'reason_code':'explicit_pending_question','idle':False}
 if info.get('state')=='busy' or active:return {**out,'state':'busy','reason_code':'active_request_not_event_frequency','running_elapsed_ms':info.get('running_elapsed_ms')}
 if info.get('state')!='ready':return {**out,'reason_code':'guest_not_observed_ready'}
 if info.get('requests_truncated') or info.get('request_count',len(rows))!=len(rows):return {**out,'reason_code':'request_inventory_incomplete'}
 terminal=[x for x in rows if x.get('state') in TERMINAL];missing=[];commits=[];s3_unverified=False
 for row in terminal:
  try:
   try:commit=committed(job,uid(info['session_id']),uid(row['request_id']))
   except (OSError,ValueError,KeyError):commit=committed_s3(job,uid(info['session_id']),uid(row['request_id']))
   commits.append(commit)
  except (OSError,ValueError,KeyError):
   missing.append(row['request_id'])
   if (base()/'exports'/uid(info['session_id'])/uid(row['request_id'])/'S3.json').is_file():s3_unverified=True
 if terminal and not missing:return {**out,'state':'ready-exported','reason_code':'all_observed_terminal_requests_have_explicit_verified_commits','scope':'declared-results-not-whole-filesystem','committed_requests':len(commits),'storage_kind':next(iter({c['storage_kind'] for c in commits})) if len({c['storage_kind'] for c in commits})==1 else 'mixed'}
 if s3_unverified:return {**out,'state':'unknown','storage_kind':'s3','reason_code':'s3_committed_export_verification_unavailable_or_changed','unacknowledged_requests':len(missing)}
 if not tracked(job):return {**out,'reason_code':'legacy_session_export_state_untracked','underlying_guest_state':'ready','unacknowledged_requests':len(missing)}
 return {**out,'state':'ready-unexported','reason_code':'explicit_verified_export_ack_missing','unacknowledged_requests':len(missing)}


def s3_ack(job,info,receipt):
 import exchange
 sid=uid(info['session_id']);rid=uid(receipt['request_id'])
 if info.get('state')!='ready' or info.get('active_request_id'):raise ValueError('No S3 ACK while busy')
 if receipt.get('scope')!='declared-terminal-results' or receipt.get('job')!=job or receipt.get('session_id')!=sid:raise ValueError('S3 receipt scope or binding mismatch')
 store=exchange.Store(exchange.config(Path.home()/'.config/nomad-boxlite-prime/exchange-reader.json'));doc=store.manifest(job,sid,rid,receipt['manifest_sha256'])
 if doc.get('scope')!='declared-terminal-results' or '.nomad-results/'+rid+'.json' not in [f['path'] for f in doc['files']]:raise ValueError('Incomplete S3 terminal export')
 prefix=exchange.identity(job,sid,rid);proofs=[]
 for item in doc['files']:
  key=prefix+'objects/'+item['sha256'];store.verify(key,item['size'],item['sha256']);proofs.append({'key':key,'size':item['size'],'sha256':item['sha256'],**store.stat(key)})
 path=base()/'exports'/sid/rid/'S3.json';path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 import idle_policy
 idle_policy.write(path,{'job':job,'session_id':sid,'request_id':rid,'receipt':receipt,'objects':proofs,'verified_at':time.time(),'storage_kind':'s3','auto_close_enabled':False})
 return committed_s3(job,sid,rid)

def committed_s3(job,sid,rid):
 doc,_=read_json(base(),base()/'exports'/uid(sid)/uid(rid)/'S3.json')
 if (doc.get('job'),doc.get('session_id'),doc.get('request_id'))!=(job,sid,rid):raise ValueError('S3 ACK binding')
 import exchange
 cfg=Path.home()/'.config/nomad-boxlite-prime/exchange-reader.json'
 if not cfg.is_file():raise ValueError('S3 backend no longer configured')
 try:
  store=exchange.Store(exchange.config(cfg));receipt=doc['receipt'];manifest=store.manifest(job,sid,rid,receipt['manifest_sha256'])
  if manifest.get('scope')!='declared-terminal-results':raise ValueError('S3 snapshot is not terminal export')
  for item in doc['objects']:
   current=store.stat(item['key'])
   if current['etag']!=item['etag'] or current['bytes']!=item['bytes'] or current['bytes']!=item['size']:raise ValueError('S3 committed object changed')
 except Exception:raise ValueError('S3 committed export unavailable or changed') from None
 return {'storage_kind':'s3','request_id':rid,'manifest_sha256':receipt['manifest_sha256'],'verified_at':doc['verified_at'],'verification_level':'full-SHA-at-ACK-and-current-object-ETag-size','auto_close_enabled':False,'replicated_backup_verified':False}

def classify(job,info):
 out=classify_base(job,info)
 if not isinstance(info,dict):return out
 import idle_policy
 return idle_policy.decorate(job,info,out)
