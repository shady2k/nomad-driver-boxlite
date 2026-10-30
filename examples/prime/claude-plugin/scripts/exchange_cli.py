"""Optional S3 CLI operations. Imported only for explicit exchange commands."""
import hashlib,json,os,stat,uuid
from pathlib import Path
import exchange

def save_receipt(d):
 root=Path.home()/'.local/state/nomad-boxlite-prime/s3-receipts';root.mkdir(mode=0o700,parents=True,exist_ok=True)
 if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:raise exchange.ExchangeError('exchange_receipt_directory')
 raw=exchange.canonical(d);p=root/(d['manifest_sha256']+'.json')
 try:fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 except FileExistsError:
  if p.read_bytes()!=raw:raise exchange.ExchangeError('exchange_receipt_conflict')
 else:
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
 return {**d,'receipt':str(p)}
def upload(w,api,args):
 root,fs,excluded=w.workspace_manifest(args.source,args.paths)
 if len(fs)>64:raise exchange.ExchangeError('exchange_initial_file_limit_64; use an explicitly selected bundle or direct upload')
 destination=args.destination or 'snapshots/'+hashlib.sha256(exchange.canonical(fs)).hexdigest()[:24]
 preview={'backend':'s3','files':len(fs),'bytes':sum(f['size'] for f in fs),'excluded':excluded,'destination':destination,'snapshot_not_mount':True}
 if not args.apply:return {**preview,'dry_run':True}
 access=w.session_access(api,args.job);info=w.active_session(api,args.job,access)
 if not info.get('capabilities',{}).get('s3_exchange'):raise exchange.ExchangeError('exchange_not_available_on_this_session')
 rid=str(uuid.uuid5(uuid.UUID(access['session_id']),hashlib.sha256(exchange.canonical({'files':fs,'destination':destination})).hexdigest()))
 d={'request_id':rid,'files':fs,'destination':destination};plan=w.endpoint('exchange',access,dict(d,operation='input-plan'))
 import multipart
 resumed_parts=0
 for item in plan['objects']:
  if w.file_digest(root/item['path'])!=item['sha256']:raise exchange.ExchangeError('exchange_source_changed')
  with w.workspace_open(root,item['path']) as f:
   import urllib.request
   if 'multipart' in item:
    checkpoint=Path.home()/'.local/state/nomad-boxlite-prime/s3-multipart'/access['session_id']/(rid+'-'+item['sha256']+'.json')
    def put_part(url,raw):
     try:
      req=urllib.request.Request(url,method='PUT',data=raw,headers={'Content-Length':str(len(raw))})
      with urllib.request.build_opener(exchange.NoRedirect).open(req,timeout=30) as response:etag=response.headers.get('ETag');response.read(1024);return etag
     except Exception:raise exchange.ExchangeError('exchange_multipart_part_unavailable') from None
    resumed_parts+=multipart.send_parts(f,item['multipart'],checkpoint,put_part)['resumed_parts'];continue
   req=urllib.request.Request(item['put_url'],method='PUT',data=f,headers={'Content-Length':str(item['size'])})
   try:
    with urllib.request.build_opener(exchange.NoRedirect).open(req,timeout=30) as r:r.read(1024)
   except Exception:raise exchange.ExchangeError('exchange_input_upload_unavailable; no prompt sent') from None
 out=w.endpoint('exchange',access,dict(d,operation='input-apply'))
 return {**preview,'committed':out.get('committed',False),'transfer_id':plan['transfer_id'],'request_id':rid,'resumed_parts':resumed_parts}
def export(w,api,args):
 access=w.session_access(api,args.job);info=w.active_session(api,args.job,access)
 if not info.get('capabilities',{}).get('s3_exchange'):raise exchange.ExchangeError('exchange_not_available_on_this_session')
 out=w.endpoint('exchange',access,{'operation':'snapshot-export' if args.snapshot else 'output-export','request_id':str(uuid.UUID(args.request_id)),'paths':args.paths,'all_outputs_declared':args.all_outputs_declared})
 if out.get('storage_kind')!='s3' or out.get('job')!=args.job or out.get('session_id')!=access['session_id']:raise exchange.ExchangeError('exchange_receipt_binding')
 saved=save_receipt(out)
 if out.get('scope')=='declared-terminal-results':
  import durable_export
  try:saved['export_state']=durable_export.s3_ack(args.job,info,out)
  except Exception:saved['export_state']={'state':'unknown','reason':'s3_ack_verification_unavailable','auto_close_enabled':False}
 return saved
def fetch(args):
 receipt=exchange.protected(args.receipt)
 if receipt.get('storage_kind')!='s3':raise exchange.ExchangeError('exchange_receipt_backend_mismatch')
 store=exchange.Store(exchange.config(Path.home()/'.config/nomad-boxlite-prime/exchange-reader.json'))
 job,sid,rid,sha=(receipt[k] for k in ('job','session_id','request_id','manifest_sha256'))
 doc=store.manifest(job,sid,rid,sha);prefix=exchange.identity(job,sid,rid)
 base=Path.home()/'.local/state/nomad-boxlite-prime/artifacts';base.mkdir(mode=0o700,parents=True,exist_ok=True)
 root=base/str(uuid.UUID(sid))/('s3-'+sha)
 for p in [base,root.parent,root,root/'files']:
  p.mkdir(mode=0o700,exist_ok=True)
  if p.is_symlink() or not stat.S_ISDIR(p.lstat().st_mode) or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:raise exchange.ExchangeError('exchange_download_directory')
 artifacts=[]
 for item in doc['files']:
  p=root/'files'/item['path'];parent=root/'files'
  for part in Path(item['path']).parts[:-1]:
   parent=parent/part;parent.mkdir(mode=0o700,exist_ok=True)
   if parent.is_symlink() or not stat.S_ISDIR(parent.lstat().st_mode):raise exchange.ExchangeError('exchange_download_path')
  partial=p.with_name(p.name+'.part');fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  h=hashlib.sha256();size=0
  try:
   with os.fdopen(fd,'wb') as f,store.open('GET',prefix+'objects/'+item['sha256']) as response:
    while True:
     raw=response.read(1048576)
     if not raw:break
     size+=len(raw)
     if size>item['size']:raise exchange.ExchangeError('exchange_download_size')
     h.update(raw);f.write(raw)
    if size!=item['size'] or h.hexdigest()!=item['sha256']:raise exchange.ExchangeError('exchange_download_hash')
    f.flush();os.fsync(f.fileno())
   if p.exists():
    if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise exchange.ExchangeError('exchange_download_conflict')
    partial.unlink()
   else:os.link(partial,p);partial.unlink()
  except Exception:
   # Retain a failed partial explicitly; never claim a full saved export.
   raise
  artifacts.append({'path':item['path'],'bytes':size,'sha256':item['sha256'],'local_path':str(p)})
 report={'backend':'s3','job':job,'session_id':sid,'request_id':rid,'manifest_sha256':sha,'artifacts':artifacts,'local_directory':str(root),'files_directory':str(root/'files'),'auto_close_enabled':False}
 (root/'manifest.json').write_bytes(exchange.canonical(report));(root/'manifest.json').chmod(0o600)
 return report

def run(w,args,api=None):
 import urllib.error
 try:
  if args.op=='exchange-fetch':return fetch(args)
  return (upload if args.op=='exchange-upload' else export)(w,api,args)
 except urllib.error.HTTPError as ex:
  retryable=ex.code in (429,500,502,503,504);reason='exchange_http_'+str(ex.code)
  try:
   d=json.loads(ex.read(8192));candidate=d.get('error')
   if isinstance(candidate,str) and __import__('re').fullmatch(r'exchange_[a-z0-9_]+',candidate):reason=candidate
  except Exception:pass
  return {'phase':'storage_unavailable' if retryable else 'exchange_failed','storage_kind':'s3','error':reason,'http_status':ex.code,'retryable':retryable,'committed':False,'retry_prompt':False,'vm_unchanged':True}
 except (exchange.ExchangeError,OSError,ValueError) as ex:
  reason=str(ex) if isinstance(ex,exchange.ExchangeError) else 'exchange_local_validation_failed'
  return {'phase':'storage_unavailable' if 'unavailable' in reason else 'exchange_failed','storage_kind':'s3','error':reason,'retryable':'unavailable' in reason,'committed':False,'retry_prompt':False,'vm_unchanged':True}
