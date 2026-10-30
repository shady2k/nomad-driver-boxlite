#!/usr/bin/env python3
"""Fixed operator-owned per-allocation adapter entrypoint. JSON is data only."""
import json,os,sys,uuid,fcntl,subprocess,platform
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE))
import universal,gateway,profiles,secure_logs,traceback,model_policy,node_paths
ROOT=node_paths.root(__file__)
LOG_DIR=None
def validate(e):
 d=universal.validate(json.loads(e['request']));sid=universal.identity(d)
 model_policy.select_model(d,gateway.protected_json(HERE/'guest/session-policy.json'))
 if e['namespace']!='boxlite-prime' or e['task_name']!='vm' or e['job_id']!=universal.parent(d):raise ValueError('Nomad identity mismatch')
 str(uuid.UUID(e['node_id']))
 if 'node_id' in d and d['node_id']!=e['node_id']:raise ValueError('Real node identity mismatch')
 if 'architecture' in d and {'x86_64':'amd64','aarch64':'arm64','arm64':'arm64'}.get(platform.machine())!=d['architecture']:raise ValueError('Scheduled host architecture does not match image architecture')
 if any(e[k]!=d[k] for k in ['image','cpus','memory_mib','disk_gib']) or e['logical_id']!=d['session_id']:raise ValueError('Request resource/identity mismatch')
 cfg=gateway.protected_json(ROOT/'node.json')
 if type(e['port']) is not int or profiles.port(e['port'])!=e['port'] or e['host']!=cfg['lan_ip']:raise ValueError('Operator session network mismatch')
 str(uuid.UUID(e['allocation_id']))
 return d,sid

def main():
 global LOG_DIR
 os.umask(0o077);raw=sys.stdin.buffer.read(32769)
 if len(raw)>32768:raise ValueError('Companion input limit')
 e=json.loads(raw);d,sid=validate(e)
 if e['operation']=='validate':print(json.dumps({'request':d,'spec_id':sid}));return
 if e['operation']!='run':raise ValueError('Companion operation')
 directory=Path(e['companion_dir']);expected=ROOT/'driver-state/companions'
 if directory.parent!=expected or len(directory.name)!=64 or directory.is_symlink():raise ValueError('Operator companion state path')
 LOG_DIR=ROOT/'sessions'/str(uuid.UUID(e['allocation_id']))/'logs'
 secure_logs.Spool(LOG_DIR).append('wrapper',json.dumps({'event':'companion_wrapper_started','allocation_id':e['allocation_id'],'request_id':d['request_id'],'session_id':d['session_id']}))
 lock=os.open(directory/'owner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);os.set_inheritable(lock,True)
 start=subprocess.check_output(['/bin/ps','-p',str(os.getpid()),'-o','lstart='],env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},text=True).strip()
 (directory/'process.json').write_text(json.dumps({'pid':os.getpid(),'start':start}))
 task=directory/'task';alloc=directory/'alloc';task.mkdir(exist_ok=True,mode=0o700);alloc.mkdir(exist_ok=True,mode=0o700)
 (task/'request.json').write_text(json.dumps(d));(alloc/'boxlite-vm.json').write_text(json.dumps(e['binding']))
 env={'HOME':str(Path.home()),'PATH':node_paths.executable_path(),'NOMAD_ALLOC_ID':e['allocation_id'],'NOMAD_JOB_ID':e['job_id'],'NOMAD_TASK_DIR':str(task),'NOMAD_ALLOC_DIR':str(alloc),'NOMAD_PORT_session':str(e['port']),'BOXLITE_SPEC_ID':sid}
 os.execve(sys.executable,[sys.executable,'-I',str(HERE/'acp_bridge.py')],env)
if __name__=='__main__':
 try:main()
 except Exception as ex:
  text=secure_logs.Redactor().text(traceback.format_exc())
  if LOG_DIR is not None:
   try:secure_logs.Spool(LOG_DIR).append('wrapper_stderr',text)
   except Exception:pass
  sys.stderr.write(text);sys.stderr.flush()
  print(json.dumps({'event':'companion_failed','error_type':type(ex).__name__,'reason':'fixed_companion_contract_or_setup_failed'}),file=sys.stderr);sys.exit(78)
