"""Exact OpenBao field in memory; public per-session UUID, no plaintext seed cache."""
from pathlib import Path
import importlib.util as _iu, sys as _sys
_ps=_iu.spec_from_file_location('profiles',Path(__file__).with_name('profiles.py'))
_pm=_iu.module_from_spec(_ps);_sys.modules['profiles']=_pm;_ps.loader.exec_module(_pm)
from profiles import profile
import hashlib
import hmac
import json
import os
from pathlib import Path
import resource
import secure_logs
import stat
import subprocess
import uuid
from urllib.parse import urlsplit

class SeedUnavailable(RuntimeError):pass

def decode_seed(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise SeedUnavailable('Invalid dispatcher seed format')
    return bytes.fromhex(value)

def derive(seed,session_id,profile_name=None):
    if not isinstance(seed,bytes) or len(seed)!=32:raise SeedUnavailable('Invalid dispatcher seed length')
    if len(profile_name)==64 and all(c in '0123456789abcdef' for c in profile_name):
        import universal
        return universal.derive(seed,session_id,profile_name)
    parent=profile(profile_name)['parent']
    domain=('nomad-boxlite-prime/openbao-v2\0boxlite-prime\0'+parent+'\0'+profile_name+'\0').encode()
    return hmac.new(seed,domain+str(uuid.UUID(session_id)).encode('ascii'),hashlib.sha256).hexdigest()

def protected_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        s=os.fstat(f.fileno())
        if s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600 or not stat.S_ISREG(s.st_mode):
            raise SeedUnavailable('Expected own environment config0600')
        return json.load(f)

def vault_environment():
    # A dedicated ONE-VARIABLE config replaces broad shell sourcing. Not a seed cache.
    p=Path.home()/'.config/nomad-boxlite-prime/environment.json'
    d=protected_json(p)
    if set(d)!={'VAULT_ADDR'}:raise SeedUnavailable('Unexpected environment configuration fields')
    expected=d['VAULT_ADDR'].rstrip('/')
    u=urlsplit(expected)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise SeedUnavailable('Expected HTTPS OpenBao origin')
    supplied=os.environ.get('VAULT_ADDR',expected).rstrip('/')
    if supplied!=expected:raise SeedUnavailable('VAULT_ADDR differs from configured OpenBao origin')
    os.environ['VAULT_ADDR']=supplied
    return supplied

def fetch_seed(timeout_seconds=8):
    # Select the configured KVv2 field directly. The CLI mount-discovery
    # preflight is unnecessary and can fail independently of this read.
    import re,urllib.request,urllib.error
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    addr=vault_environment();operator=protected_json(Path.home()/'.config/nomad-boxlite-prime/operator.json')
    mount=operator['seed_mount'];secret_path=operator['seed_path']
    if not isinstance(mount,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',mount):raise SeedUnavailable('Invalid operator KVv2 mount')
    if not isinstance(secret_path,str) or any(not re.fullmatch(r'[A-Za-z0-9_-]+',p) for p in secret_path.split('/')):raise SeedUnavailable('Invalid operator KVv2 secret selector')
    path=Path.home()/'.vault-token';fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        s=os.fstat(f.fileno())
        if s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600 or not stat.S_ISREG(s.st_mode):raise SeedUnavailable('Expected existing protected OpenBao auth')
        auth=f.read().strip()
    if not auth:raise SeedUnavailable('Existing OpenBao auth empty')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**kw):raise SeedUnavailable('OpenBao redirect refused')
    request=urllib.request.Request(addr+'/v1/'+mount+'/data/'+secret_path,headers={'X-Vault-Token':auth})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect).open(request,timeout=max(timeout_seconds,1e-9)) as response:raw=response.read(65537)
    except urllib.error.HTTPError as e:raise SeedUnavailable('OpenBao dispatcher KVv2 read HTTP '+str(e.code)+'; no dispatch/retry') from None
    except SeedUnavailable:raise
    except Exception as e:raise SeedUnavailable('OpenBao dispatcher KVv2 read unavailable ('+type(e).__name__+'); no dispatch/retry') from None
    if len(raw)>65536:raise SeedUnavailable('OpenBao dispatcher KVv2 response limit')
    try:value=json.loads(raw)['data']['data']['BOXLITE_DISPATCHER_SEED']
    except Exception:raise SeedUnavailable('OpenBao selected dispatcher field missing or invalid KVv2 response') from None
    return decode_seed(value)

def access(session_id,profile_name=None):
    sid=str(uuid.UUID(session_id));return {'session_id':sid,'token':derive(fetch_seed(),sid,profile_name)}
