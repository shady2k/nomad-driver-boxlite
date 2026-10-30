"""Per-session authenticated bridge adapter; URLs never enter logs/receipts."""
import json,hashlib
from pathlib import Path
import exchange,multipart
class Bridge:
 def __init__(self,call,job,sid,config_path):self.call=call;self.job=job;self.sid=sid;self.config_path=config_path
 def guest(self,d):
  status,raw=self.call('/artifact-transfer','POST',json.dumps(d).encode());r=json.loads(raw)
  if status!=200 or r.get('error'):raise exchange.ExchangeError('exchange_guest_transfer_failed')
  return r
 def __call__(self,url,method,body):
  if url!='/exchange':
   code,raw=self.call(url,method,body)
   if url=='/status' and code==200:
    info=json.loads(raw);info.setdefault('capabilities',{})['s3_exchange']=Path(self.config_path).is_file();raw=json.dumps(info).encode()
   return code,raw
  if not Path(self.config_path).is_file():return 409,b'{"error":"exchange_not_configured","backend":"direct"}'
  try:
   d=json.loads(body);rid=str(__import__('uuid').UUID(d['request_id']));prefix=exchange.identity(self.job,self.sid,rid);store=exchange.Store(exchange.config(self.config_path));mp=multipart.Manager(store,Path(self.config_path).parent/'exchange-checkpoints');op=d['operation']
   if op in ('input-plan','input-apply'):
    fs=exchange.files(d['files']);destination=exchange.path(d['destination']);transfer=hashlib.sha256(json.dumps({'destination':destination,'files':fs},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if op=='input-plan':
     objects=[]
     for f in fs:
      key=prefix+'inputs/'+f['sha256'];item={'path':f['path'],'size':f['size'],'sha256':f['sha256']}
      item.update({'multipart':mp.plan(key,f['size'],f['sha256'])} if f['size']>multipart.PART else {'put_url':store.url('PUT',key)})
      objects.append(item)
     out={'transfer_id':transfer,'objects':objects,'expires_in':300}
    else:
     for f in fs:
      key=prefix+'inputs/'+f['sha256']
      if f['size']>multipart.PART:mp.finish(key,f['size'],f['sha256'])
      store.verify(key,f['size'],f['sha256'])
     self.guest({'operation':'upload-start','transfer_id':transfer,'destination':destination,'files':len(fs),'bytes':sum(f['size'] for f in fs)})
     for f in fs:self.guest({'operation':'s3-pull','transfer_id':transfer,**f,'url':store.url('GET',prefix+'inputs/'+f['sha256']),'origin':store.cfg['endpoint']})
     out=self.guest({'operation':'upload-commit','transfer_id':transfer})
   elif op in ('output-export','snapshot-export'):
    code,raw=self.call('/status','GET',b'');info=json.loads(raw)
    rec=next((q for q in info.get('requests',[]) if q.get('request_id')==rid),None)
    if code!=200 or info.get('session_id')!=self.sid or info.get('state')!='ready' or info.get('active_request_id') or (op=='output-export' and (not rec or rec.get('state') not in ('completed','cancelled','failed_no_retry','uncertain_no_retry'))):raise exchange.ExchangeError('exchange_terminal_request_required')
    paths=d['paths']
    if not isinstance(paths,list) or not 1<=len(paths)<=4096:raise exchange.ExchangeError('exchange_file_count')
    if d.get('all_outputs_declared') is not True or (op=='output-export' and ('.nomad-results/'+rid+'.json') not in paths):raise exchange.ExchangeError('exchange_complete_result_declaration_required')
    fs=[]
    for start in range(0,len(paths),128):fs+=self.guest({'operation':'manifest','paths':paths[start:start+128]})['files']
    fs=exchange.files(fs)
    for f in fs:
     key=prefix+'objects/'+f['sha256'];payload={'operation':'s3-push',**f,'origin':store.cfg['endpoint']}
     if f['size']>multipart.PART:payload['multipart']=mp.plan(key,f['size'],f['sha256'])
     else:payload['url']=store.url('PUT',key)
     self.guest(payload)
     if f['size']>multipart.PART:mp.finish(key,f['size'],f['sha256'])
    out=store.commit(self.job,self.sid,rid,fs,scope='selected-snapshot' if op=='snapshot-export' else 'declared-terminal-results')
   else:raise exchange.ExchangeError('exchange_operation')
   return 200,json.dumps(out).encode()
  except exchange.ExchangeError as e:
   reason=str(e);unavailable=any(x in reason for x in ('unavailable','http_500','http_502','http_503','http_504','http_429'))
   return 503 if unavailable else 409,json.dumps({'error':reason,'storage_kind':'s3','storage_unavailable':unavailable,'retryable':unavailable,'committed':False,'retry_prompt':False}).encode()
  except Exception:return 409,b'{"error":"exchange_invalid_or_unavailable","retry_prompt":false}'
