#!/usr/bin/env python3
"""Fixed field-only node credential provider. Output must be captured in memory.
No shell, no caller-selected field/path, no cache, no secret-bearing error output.
"""
import json,os,stat,subprocess,sys,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent.parent/"examples/prime"))
import node_paths
root=node_paths.root(__file__)
stage='config'
try:
 def read(path):
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
  with os.fdopen(fd) as f:
   s=os.fstat(f.fileno())
   if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600:raise ValueError('private file')
   return f.read(16384)
 cfg=json.loads(read(root/'node.json'));operator=json.loads(read(root/'operator.json'));stage='auth_file';auth=read(cfg['vault_token_file']).strip();stage='bao_process'
 env={'HOME':str(Path.home()),'PATH':node_paths.executable_path(),'VAULT_ADDR':cfg['vault_addr'],'VAULT_TOKEN':auth,'VAULT_CLIENT_TIMEOUT':'8s','VAULT_MAX_RETRIES':'0'}
 p=subprocess.run([operator.get('bao_binary') or shutil.which('bao'),'kv','get','-mount='+operator['rest_key_mount'],'-field=BOXLITE_REST_API_KEY',operator['rest_key_path']],env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
 stage='selected_field';key=p.stdout.decode().strip()
 if p.returncode:
  import re
  codes=re.findall(rb'Code: ([0-9]+)',p.stderr)
  why=[w for w in ['connection refused','no such host','certificate','permission denied','operation not permitted','timeout','i/o timeout','context deadline','no route','sealed','127.0.0.1','https://','http://','unexpected eof','network is unreachable'] if w in p.stderr.decode(errors='replace').lower()]
  ips=re.findall(rb'dial tcp ([0-9a-fA-F:.\[\]]+)',p.stderr)
  sys.stderr.write('connect_endpoints='+str([x.decode() for x in ips])+'\n')
  sys.stderr.write('bao_exit='+str(p.returncode)+' http_codes='+str([x.decode() for x in codes])+' classes='+str(why)+'\n')
 if p.returncode or len(key)!=64 or any(x not in '0123456789abcdef' for x in key):raise ValueError('selected field unavailable')
 sys.stdout.write(key)
except Exception as e:
 sys.stderr.write('provider_stage='+stage+' exception='+type(e).__name__+'\n')
 sys.stderr.write('selected node credential unavailable; fail closed\n');sys.exit(78)
