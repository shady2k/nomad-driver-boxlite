#!/usr/bin/env python3
"""Rootless bootstrap: selected API credential in memory/environment, never argv."""
import os,subprocess,sys,json,stat
from urllib.parse import urlsplit
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent.parent/"examples/prime"))
import node_paths
root=node_paths.root(__file__)
bin_dir=Path(__file__).resolve().parent
fd=os.open(root/'operator.json',os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd) as f:
 st=os.fstat(f.fileno())
 if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):raise SystemExit('private operator configuration required')
 operator=json.load(f)
runtime_dir=node_paths.trusted_runtime(operator)
u=urlsplit(operator['rest_endpoint'])
if u.scheme!='http' or u.hostname!='127.0.0.1' or not u.port or u.username or u.password or u.path or u.query or u.fragment:raise SystemExit('explicit loopback REST origin required')
p=subprocess.run([sys.executable,'-I',str(bin_dir/'read-rest-key.py')],stdin=subprocess.DEVNULL,capture_output=True,timeout=12)
if p.returncode:
 sys.stderr.write('credential_provider_exit='+str(p.returncode)+' '+p.stderr.decode()[:300]+'\n')
 raise SystemExit('selected node credential unavailable; fail closed')
key=p.stdout.decode().strip()
if len(key)!=64 or any(x not in '0123456789abcdef' for x in key):raise SystemExit('invalid selected node credential')
env={k:os.environ[k] for k in ['HOME','PATH','TMPDIR'] if k in os.environ};env['BOXLITE_SERVE_API_KEY']=key
if runtime_dir is not None:env['BOXLITE_RUNTIME_DIR']=runtime_dir
args=[str(bin_dir/'boxlite'),'serve','--host','127.0.0.1','--port',str(u.port),'--home',str(root/'runtime')]
privileged=operator.get('guest_privileged_default',False)
if type(privileged) is not bool:raise SystemExit('guest_privileged_default must be boolean')
if privileged:args.append('--guest-privileged-default')
os.execve(bin_dir/'boxlite',args,env)
