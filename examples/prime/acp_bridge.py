#!/usr/bin/env python3
"""Application-only ACP bridge over native REST stdio. NO VM lifecycle/placement."""
import socket,ipaddress
from urllib.parse import urlsplit
import collections,importlib.util,json,os,pwd,resource,shlex,signal,stat,subprocess,sys,threading,time,uuid
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import profiles,contract,dispatcher_auth,channel,gateway,events,universal,secure_logs,traceback,observability,model_policy,node_paths
OWNER_HOME=Path(pwd.getpwuid(os.getuid()).pw_dir)
ROOT=node_paths.root(__file__)
STAGE='entry'
BOOTSTRAP_SECONDS=60
REDACTOR=secure_logs.Redactor()
NAMES=['provision.sh','uv.lock','package.json','package-lock.json','models.json','settings.json','guest.py','server.mjs','session.mjs','artifacts.py','session-policy.json']
def config():
 d=gateway.protected_json(ROOT/'node.json')
 if set(d)!={'lan_ip','source_ip','vault_addr','vault_token_file','rest_key_command','stdio_binary','envconsul_binary','python_binary'}:raise ValueError('Node configuration shape')
 return d

def identity():
 aid=str(uuid.UUID(os.environ['NOMAD_ALLOC_ID']));req=contract.validate(json.loads((Path(os.environ['NOMAD_TASK_DIR'])/'request.json').read_text()))
 if 'image' in req:
  if universal.identity(req)!=os.environ.get('BOXLITE_SPEC_ID'):raise ValueError('Registered spec identity binding')
  if os.environ['NOMAD_JOB_ID'].split('/dispatch-',1)[0]!=universal.parent(req):raise ValueError('Registered request parent binding')
 else:
  if req['profile']!=os.environ['BOXLITE_PROFILE']:raise ValueError('Registered profile binding')
  if os.environ['NOMAD_JOB_ID'].split('/dispatch-',1)[0]!=profiles.profile(req['profile'])['parent']:raise ValueError('Registered parent binding')
 if '/dispatch-' in os.environ['NOMAD_JOB_ID']:
  epoch=os.environ['NOMAD_JOB_ID'].split('/dispatch-',1)[1].split('-',1)[0]
  if not epoch.isdecimal() or time.time()-int(epoch)<-30:raise ValueError('Invalid session creation time')
 return aid,req

CONTEXT_KEYS={'NOMAD_ALLOC_ID','NOMAD_ALLOC_DIR','NOMAD_TASK_DIR','NOMAD_JOB_ID','NOMAD_PORT_session','BOXLITE_PROFILE'}
def restore_context(aid):
 aid=str(uuid.UUID(aid));ctx=gateway.protected_json(ROOT/'sessions'/aid/'context.json')
 if set(ctx) not in (CONTEXT_KEYS,(CONTEXT_KEYS-{'BOXLITE_PROFILE'})|{'BOXLITE_SPEC_ID'}) or ctx['NOMAD_ALLOC_ID']!=aid or any(not isinstance(x,str) for x in ctx.values()):raise ValueError('Allocation context binding')
 os.environ.update(ctx)

def selected_rest_key(cfg):
 import re
 for attempt in range(2):
  try:
   result=subprocess.run(cfg['rest_key_command'],stdin=subprocess.DEVNULL,capture_output=True,timeout=12)
   text=result.stderr.decode(errors='replace').lower()
   classes=[n for n in ['permission denied','operation not permitted','certificate','no route','network is unreachable','timeout','context deadline','unexpected eof','connection refused','no such host'] if n in text]
   codes=re.findall(r"http_codes=\[([^]]*)\]",text)
   http_codes=re.findall(r"\b[1-5][0-9]{2}\b",' '.join(codes))
   stage=re.search(r'provider_stage=([a-z_]+)',text)
   diagnostic={'provider_exit':result.returncode,'provider_stage':stage.group(1) if stage else 'unknown','classes':classes,'http_codes':http_codes}
   if result.returncode==0:
    key=result.stdout.decode().strip()
    if len(key)==64 and all(c in '0123456789abcdef' for c in key):return key
    diagnostic['classes']=['invalid_selected_field']
  except subprocess.TimeoutExpired:
   diagnostic={'provider_exit':None,'provider_stage':'provider_deadline','classes':['timeout'],'http_codes':[]}
  denied=any(n in diagnostic['classes'] for n in ['permission denied','operation not permitted','certificate','no route','network is unreachable']) or any(c in diagnostic['http_codes'] for c in ['401','403'])
  transient=any(n in diagnostic['classes'] for n in ['timeout','context deadline','unexpected eof','connection refused','no such host'])
  if attempt==0 and transient and not denied:
   print(json.dumps({'event':'credential_read_retry','layer':'secret_provider','stage':'rest_credential','diagnostic':diagnostic,'read_only':True,'attempt':2}),flush=True)
   time.sleep(.2);continue
  raise RuntimeError('selected_node_credential_unavailable '+json.dumps(diagnostic))

def terminal_summary(record, binding=None):
 """Fixed companion-to-driver contract: categorical scalars, no raw output."""
 directory=Path(os.environ.get('NOMAD_TASK_DIR','/invalid')).parent
 if directory.parent != ROOT/'driver-state/companions' or directory.is_symlink():return
 aid=str(uuid.UUID(os.environ['NOMAD_ALLOC_ID']))
 allowed=['stage','exception_type','error_type','reason','deadline_seconds','elapsed_seconds','exit_code','signal']
 safe={k:str(record[k])[:160] for k in allowed if k in record and isinstance(record[k],(str,int,float))}
 stages={'startup','repo_clone','repo_checkout','initial_command','prime_daemon_start','acp_server_start','node_config','payload_identity','envconsul_start','allocation_context','child_config','child_payload','rest_credential','entry'}
 if safe.get('stage') not in stages:safe['stage']='unknown'
 types={'TimeoutError','RuntimeError','ValueError','OSError','FileNotFoundError','PermissionError','KeyboardInterrupt','BrokenPipeError','ConnectionError'}
 for k in ['exception_type','error_type']:
  if k in safe and safe[k] not in types:safe[k]='UnknownError'
 for k in ['deadline_seconds','elapsed_seconds','exit_code','signal']:
  if k in safe:
   try:
    value=float(safe[k])
    if not __import__('math').isfinite(value):raise ValueError()
   except ValueError:safe.pop(k)

 # Reject arbitrary reason text; only guest categorical reasons are public.
 if safe.get('reason') not in ['deadline_exceeded','command_failed','bootstrap_failed','interrupted','transport_failed']:safe['reason']='bootstrap_failed'
 data={'version':1,'allocation_id':aid,'vm_id':(binding or {}).get('vm_id',''),'timestamp':time.time(),'summary':safe}
 if (directory/'terminal-summary.json').exists():return # preserve the first primary failure, not a later generic exit
 temp=directory/'terminal-summary.tmp'
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f);f.flush();os.fsync(f.fileno())
 os.replace(temp,directory/'terminal-summary.json')


def application_ready_receipt(binding):
 directory=Path(os.environ['NOMAD_TASK_DIR']).parent
 if directory.parent != ROOT/'driver-state/companions' or directory.is_symlink():raise ValueError('Invalid companion ready receipt directory')
 data={'version':1,'allocation_id':str(uuid.UUID(os.environ['NOMAD_ALLOC_ID'])),'vm_id':binding['vm_id']}
 if (directory/'application-ready.json').exists():
  if gateway.protected_json(directory/'application-ready.json')!=data:raise ValueError('Ready receipt identity mismatch')
  return
 temp=directory/('application-ready-'+uuid.uuid4().hex+'.tmp')
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f);f.flush();os.fsync(f.fileno())
 os.replace(temp,directory/'application-ready.json')


def provider_host_aliases():
 policy=gateway.protected_json(HERE/'guest/session-policy.json')
 models=gateway.protected_json(HERE/'guest/models.json')
 provider=models['providers'][policy['provider']];url=urlsplit(provider['baseUrl'])
 if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in (None,443):raise ValueError('Unsupported operator provider transport')
 host=url.hostname
 try:ipaddress.ip_address(host);return {}
 except ValueError:pass
 operator=gateway.protected_json(ROOT/'operator.json');rules=operator.get('outbound_allow_net',[])
 if host not in rules and not operator.get('outbound_allow_all'):raise ValueError('Provider hostname is not operator-approved')
 addresses=sorted({a[4][0] for a in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM) if a[0]==socket.AF_INET})
 private=[a for a in addresses if ipaddress.ip_address(a).is_private]
 if not private:return {}
 networks=[]
 for rule in rules:
  try:networks.append(ipaddress.ip_network(rule,strict=False))
  except ValueError:pass
 for value in private:
  ip=ipaddress.ip_address(value)
  if ip.is_loopback or ip.is_link_local or ip.is_unspecified or not any(ip in network for network in networks):
   raise RuntimeError('provider_private_destination_requires_explicit_operator_allowlist')
 return {host:private}


def required_secret_material():
 REDACTOR.add(*(os.environ.get(name) for name in ['LLM_API_KEY','BOXLITE_DISPATCHER_SEED','VAULT_TOKEN','NOMAD_TOKEN']))
 print(json.dumps({'event':'secret_environment_checked','layer':'secret_provider','stage':'child_payload','pid':os.getpid(),'required_fields_present':{name:bool(os.environ.get(name)) for name in ['LLM_API_KEY','BOXLITE_DISPATCHER_SEED']}}),flush=True)
 missing=[name for name in ['LLM_API_KEY','BOXLITE_DISPATCHER_SEED'] if not os.environ.get(name)]
 if missing:raise RuntimeError('required_secret_fields_missing: '+','.join(missing))
 # Validate all required material before removing either value from the environment.
 key=os.environ['LLM_API_KEY'];seed=dispatcher_auth.decode_seed(os.environ['BOXLITE_DISPATCHER_SEED'])
 for name in ['LLM_API_KEY','BOXLITE_DISPATCHER_SEED']:os.environ.pop(name)
 return key,seed

def child():
 global STAGE
 STAGE='allocation_context';restore_context(sys.argv[2])
 STAGE='child_config';resource.setrlimit(resource.RLIMIT_CORE,(0,0));cfg=config();log_policy=observability.logging_policy(ROOT/'logging.json',gateway.protected_json);STAGE='child_payload';aid,req=identity()
 selected_model=model_policy.select_model(req,gateway.protected_json(HERE/'guest/session-policy.json'))
 print(json.dumps({'event':'model_selected','layer':'application','stage':'model_policy','model':selected_model}),flush=True)
 provider_hosts=provider_host_aliases()
 key,seed=required_secret_material()
 token=dispatcher_auth.derive(seed,req['session_id'],universal.identity(req) if 'image' in req else req['profile']);del seed
 REDACTOR.add(token)
 print(json.dumps({'event':'secret_bootstrap_ready','layer':'application','stage':'secrets_ready'}),flush=True)
 binding=Path(os.environ['NOMAD_ALLOC_DIR'])/'boxlite-vm.json'
 deadline=time.monotonic()+180
 while not binding.exists():
  if time.monotonic()>deadline:raise RuntimeError('native_driver_binding_timeout')
  time.sleep(.1)
 b=gateway.protected_json(binding)
 if b.get('allocation_id')!=aid or b.get('task')!='vm':raise ValueError('Native allocation binding mismatch')
 gateway.LAN_IP=cfg['lan_ip'];gateway.SOURCE_IP=cfg['source_ip']
 owned=ROOT/'sessions'/aid;owned.mkdir(parents=True,exist_ok=True,mode=0o700)
 for n in ['binding.json']:(owned/n).write_text(json.dumps(b));os.chmod(owned/n,0o600)
 STAGE='rest_credential';rest_key=selected_rest_key(cfg);REDACTOR.add(rest_key)
 STAGE='guest_bootstrap' # Later transport errors are not pre-session credential failures.
 transport=channel.Channel();journal=events.Journal(owned/'events.jsonl');proc=None;secrets=[key,token,rest_key]
 def safe(d):
  raw=json.dumps(d)
  for secret in secrets:raw=raw.replace(secret,'[REDACTED]').replace(json.dumps(secret)[1:-1],'[REDACTED]')
  return raw
 def event(d):
  d={'timestamp':time.time(),'allocation_id':aid,'vm_id':b['vm_id'],'request_id':req['request_id'],'session_id':req['session_id'],**d}
  raw=safe(d);print(raw,flush=True)
  with (owned/'bridge.jsonl').open('a') as f:f.write(raw+'\n')
  journal.append(json.loads(raw))
 def stop(*_):raise KeyboardInterrupt()
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:
  with gateway.Gateway(token,req['session_id'],(__import__('exchange_bridge').Bridge(transport.call,os.environ['NOMAD_JOB_ID'],req['session_id'],ROOT/'exchange-writer.json') if (ROOT/'exchange-writer.json').is_file() else transport.call),profiles.port(os.environ['NOMAD_PORT_session']),journal=journal):
   event({'event':'gateway_listening','layer':'application','stage':'guest_bootstrap'})
   proc=subprocess.Popen([cfg['stdio_binary'],'-endpoint',gateway.protected_json(ROOT/'operator.json')['rest_endpoint'],'-binding',str(binding),'-allocation',aid,'-key-env','BOXLITE_REST_API_KEY'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':'/usr/bin:/bin','HOME':str(OWNER_HOME),'BOXLITE_REST_API_KEY':rest_key})
   transport.attach(proc)
   assets={n:(HERE/'guest'/n).read_text() for n in NAMES}
   assets['models.json']=json.dumps(model_policy.session_models(json.loads(assets['models.json']),gateway.protected_json(HERE/'guest/session-policy.json'),req['session_id']))
   envelope={'key':key,'request':req,'token':token,'model':selected_model,'provider_hosts':provider_hosts,'assets':assets}
   proc.stdin.write((json.dumps(envelope)+'\n').encode());proc.stdin.flush();del envelope
   errors=collections.deque(maxlen=8)
   def err():
    while True:
     chunk=proc.stderr.readline()
     if not chunk:return
     errors.append(chunk)
     sys.stderr.write(REDACTOR.text(chunk.decode(errors='replace')));sys.stderr.flush()
   threading.Thread(target=err,daemon=True).start()
   def read():
    try:
     while True:
      raw=proc.stdout.readline(131073)
      if not raw:return
      if len(raw)>131072:continue
      try:o=json.loads(raw)
      except ValueError:
       sys.stdout.write(REDACTOR.text(raw.decode(errors='replace')));sys.stdout.flush();continue
      if not isinstance(o,dict):continue
      if transport.receive(json.loads(safe(o))):continue
      metadata=observability.project(o,{'allocation_id':aid,'vm_id':b['vm_id'],'session_id':req['session_id'],'request_id':req['request_id']},chunk_metadata=log_policy['chunk_metadata'])
      if metadata:
       raw=safe(metadata);print(raw,flush=True)
       with (owned/'bridge.jsonl').open('a') as f:f.write(raw+'\n')
       if o.get('event')=='acp_trace':continue # Runtime traces go to logs, not payload replay journal.
      if o.get('event') in ['stage_started','stage_finished','stage_failed','guest_bootstrap_failed']:
       allowed=['event','layer','stage','timestamp','deadline_seconds','attempt','elapsed_seconds','exit_code','signal','exception_type','reason','error_classes']
       event({k:v for k,v in o.items() if k in allowed})
       if o.get('event') in ['stage_failed','guest_bootstrap_failed'] or (o.get('event')=='stage_finished' and o.get('exit_code') not in (None,0)):
        terminal_summary({**o,'reason':o.get('reason','command_failed')},b)
       continue
      if o.get('event') in ['agent_progress','guest_session_ready','result','request_accepted','request_running','request_terminal','progress','question','answer_recorded','answer_delivered','assistant_delta']:
       public={k:v for k,v in o.items() if k in ['event','session_id','acp_session_id','backend_session_id','record_id','state','reason','requests','request_id','kind','status','question_id','message','schema','answer_id','outcome','text','error','tool_call_id_sha256','elapsed_ms','error_code','timestamp','message_kind','entries','plan_entry_count']}
       if o.get('event')=='request_terminal':public['terminal_metadata']=observability.terminal_metadata(o.get('terminal_metadata'))
       journal.append(json.loads(safe(public)))
       if o.get('event') in ['guest_session_ready','result']:
        if o.get('event')=='guest_session_ready':application_ready_receipt(b)
        raw=safe(public);print(raw,flush=True)
        with (owned/'bridge.jsonl').open('a') as f:f.write(raw+'\n')
    finally:transport.close()
   reader=threading.Thread(target=read,daemon=True);reader.start()
   code=proc.wait();reader.join(2)
   if code:event({'event':'acp_transport_failed','layer':'guest_stdio','exit_code':code,'error':b''.join(errors).decode(errors='replace')[-2048:]})
   return 44 if code else 0
 finally:
  transport.close()
  if proc and proc.poll() is None:
   proc.terminate()
   try:proc.wait(3)
   except subprocess.TimeoutExpired:proc.kill();proc.wait()
  # VM stop/destroy belongs to native Nomad driver, NEVER this bridge.

def main():
 global STAGE
 os.umask(0o077)
 if len(sys.argv)==3 and sys.argv[1]=='--secret-child':
  sys.stdout=secure_logs.RedactedWriter(sys.stdout,REDACTOR);sys.stderr=secure_logs.RedactedWriter(sys.stderr,REDACTOR)
  return child()
 if len(sys.argv)!=1:raise ValueError('No request-selected host arguments')
 STAGE='node_config';cfg=config();STAGE='payload_identity';aid,req=identity();STAGE='envconsul_start';owned=ROOT/'sessions'/aid;owned.mkdir(parents=True,exist_ok=True,mode=0o700)
 ctx={k:os.environ[k] for k in (CONTEXT_KEYS if 'BOXLITE_PROFILE' in os.environ else (CONTEXT_KEYS-{'BOXLITE_PROFILE'})|{'BOXLITE_SPEC_ID'})};f=owned/'context.json';f.write_text(json.dumps(ctx));f.chmod(0o600)
 hcl=(HERE/'envconsul.hcl').read_text().replace('vault {','vault {\n vault_agent_token_file = '+json.dumps(cfg['vault_token_file']),1)
 file=owned/'envconsul.hcl';file.write_text(hcl);file.chmod(0o600)
 command=[cfg['envconsul_binary'],'-config='+str(file),'-once','-vault-renew-token=false','-log-level=warn','--',shlex.join([cfg['python_binary'],'-I',str(HERE/'acp_bridge.py'),'--secret-child',aid])]
 env={k:v for k,v in os.environ.items() if k.startswith('NOMAD_') or k in ('BOXLITE_PROFILE','BOXLITE_SPEC_ID')};env.update(HOME=str(OWNER_HOME),PATH=node_paths.executable_path(),VAULT_ADDR=cfg['vault_addr'])
 p=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
 ready=threading.Event();done=threading.Event();secrets_ready=threading.Event();capture_errors=[]
 spool=secure_logs.Spool(owned/'logs',REDACTOR);delivery_unavailable=set()
 def record(d,emit=True):
  d={'timestamp':time.time(),'allocation_id':aid,'request_id':req.get('request_id'),'session_id':req.get('session_id'),**d}
  raw=json.dumps(d)
  if emit:print(raw,flush=True)
  with (owned/'bootstrap.jsonl').open('a') as f:f.write(raw+'\n')
 record({'event':'secret_bootstrap_started','layer':'application','stage':'envconsul_start','deadline_seconds':BOOTSTRAP_SECONDS})
 def output(stream,kind):
  for raw in iter(stream.readline,b''):
   try:
    target=sys.stdout if kind=='stdout' else sys.stderr
    text=secure_logs.capture_and_mirror(spool,target,kind,raw,delivery_unavailable)
   except Exception as e:
    capture_errors.append(type(e).__name__+': '+str(e));ready.set()
    if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
    return
   try:
    d=json.loads(text)
    if not isinstance(d,dict):continue
    d={k:v for k,v in d.items() if k in ['event','layer','stage','error_type','reason','outcome','exit_code','session_id','state','acp_session_id','backend_session_id','record_id','requests','diagnostic','attempt','read_only','pid','required_fields_present']}
    if d.get('event')=='secret_bootstrap_ready':secrets_ready.set();ready.set()
    if d:record(d,emit=False)
   except (ValueError,UnicodeError):
    text=raw.decode(errors='replace').lower()
    record(observability.plain_output_metadata(kind,text,secrets_ready.is_set()))
 readers=[threading.Thread(target=output,args=(stream,kind),daemon=True) for stream,kind in [(p.stdout,'stdout'),(p.stderr,'stderr')]]
 for t in readers:t.start()
 def exited():
  code=p.wait()
  # Drain both streams before signaling failure so the primary child cause is durable.
  for t in readers:t.join(2)
  record({'event':'secret_provider_exited','layer':'secret_provider','stage':'envconsul_start','exit_code':code})
  done.set();ready.set()
 exit_thread=threading.Thread(target=exited,daemon=True);exit_thread.start()
 def stop(*_):raise KeyboardInterrupt()
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:
  if not ready.wait(BOOTSTRAP_SECONDS):
   STAGE='envconsul_bootstrap_timeout'
   raise TimeoutError('Secret bootstrap did not launch ACP child within60s; no automatic retry')
  if capture_errors:raise RuntimeError('diagnostic_capture_failed: '+'; '.join(capture_errors))
  if done.is_set() and not secrets_ready.is_set():
   STAGE='envconsul_bootstrap_failed'
   raise RuntimeError('Secret provider exited before readiness; inspect redacted bootstrap events')
  code=p.wait()
  exit_thread.join(5)
  for t in readers:t.join(2)
  if capture_errors:raise RuntimeError('diagnostic_capture_failed: '+'; '.join(capture_errors))
  return code
 finally:
  if p.poll() is None:
   os.killpg(p.pid,signal.SIGTERM)
   try:p.wait(5)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
  for t in readers:t.join(1)
  exit_thread.join(5)
  p.stdout.close();p.stderr.close()

if __name__=='__main__':
 try:sys.exit(main())
 except KeyboardInterrupt:sys.exit(143)
 except Exception as e:
  text=REDACTOR.text(traceback.format_exc());sys.stderr.write(text);sys.stderr.flush()
  if not (len(sys.argv)==3 and sys.argv[1]=='--secret-child'):
   try:secure_logs.Spool(ROOT/'sessions'/str(uuid.UUID(os.environ['NOMAD_ALLOC_ID']))/'logs',REDACTOR).append('wrapper_stderr',text)
   except Exception:pass
  try:terminal_summary({'stage':STAGE,'error_type':type(e).__name__,'reason':'bootstrap_failed'})
  except Exception:pass
  message=str(e)[:512]
  for name in ['LLM_API_KEY','BOXLITE_DISPATCHER_SEED','BOXLITE_REST_API_KEY']:
   secret=os.environ.get(name)
   if secret:message=message.replace(secret,'[REDACTED]')
  report={'event':'acp_bridge_failed','layer':'application','stage':STAGE,'error_type':type(e).__name__,'reason':message,'do_not_retry':True}
  try:
   aid=str(uuid.UUID(os.environ.get('NOMAD_ALLOC_ID') or sys.argv[2]));out=ROOT/'sessions'/aid;out.mkdir(parents=True,exist_ok=True,mode=0o700)
   file=out/'diagnostic.json'
   if not file.exists():file.write_text(json.dumps(report));file.chmod(0o600)
  except Exception:pass
  print(json.dumps(report),flush=True);sys.exit(44)
