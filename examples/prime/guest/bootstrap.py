"""Guest-only stdin bootstrap. No actual credentials written to disk."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ASSETS = {
    'provision.sh':'/opt/worker/assets/provision.sh',
    'uv.lock':'/opt/worker/assets/uv.lock',
    'package.json':'/opt/worker/acpx/package.json',
    'package-lock.json':'/opt/worker/acpx/package-lock.json',
    'models.json':'/root/.prime/nomad-persistent/models.json',
    'settings.json':'/root/.prime/nomad-persistent/settings.json',
    'guest.py':'/opt/worker/guest.py',
    'server.mjs':'/opt/worker/acpx/server.mjs',
    'session.mjs':'/opt/worker/acpx/session.mjs',
    'artifacts.py':'/opt/worker/artifacts.py',
    'session-policy.json':'/opt/worker/session-policy.json',
}

def read_line(fd,limit):
    # No BufferedReader: the daemon pump must not hold its lock at interpreter exit.
    out=bytearray()
    while len(out)<limit:
        b=os.read(fd,1)
        if not b or b==b'\n':return bytes(out)
        out.extend(b)
    raise ValueError('Bootstrap line limit')


KERNEL_DEFAULT_MODULES=('requests','httpx','yaml','tomli','dotenv','pandas','numpy','scipy','bs4','lxml','pydantic','tyro','rlm','agent_message','dill')
def kernel_preflight(env):
    code='import importlib; [importlib.import_module(name) for name in '+repr(KERNEL_DEFAULT_MODULES)+']'
    try:
        result=subprocess.run(['/opt/worker/prime/prime-agent-runtime/.venv/bin/python','-I','-c',code],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        return result.returncode==0
    except (OSError,subprocess.TimeoutExpired):return False


def docker_preflight(env):
    """Image opt-in only; no secrets, host socket, or caller-selected command."""
    if not Path('/opt/worker/.docker-default-v1').is_file():return True
    try:
        result=subprocess.run(['/opt/worker/start-docker.sh'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=40)
        return result.returncode==0
    except (OSError,subprocess.TimeoutExpired):return False


def main():
    # The host constructs assets; request files cannot choose these host/guest paths.
    envelope = json.loads(read_line(sys.stdin.fileno(),1024*1024))
    if set(envelope['assets']) != set(ASSETS):
        raise ValueError('Unexpected bootstrap assets')
    os.umask(0o077)
    for name,path in ASSETS.items():
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(envelope['assets'][name])
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/root','DEBIAN_FRONTEND':'noninteractive'}
    # stdin is explicitly closed for provisioning. The key remains only in this
    # process's input object, never inherited by apt/curl/npm/uv.
    if not all(Path(p).is_file() for p in ('/opt/worker/.runtime-v1','/opt/worker/.kernel-defaults-v1')):
        print(json.dumps({'event':'result','outcome':'prepared_runtime_missing','model_calls_attempted':0}));return 44
    if not kernel_preflight(env):
        print(json.dumps({'event':'guest_bootstrap_failed','layer':'guest_bootstrap','stage':'child_config','exception_type':'RuntimeError','reason':'bootstrap_failed','error_code':'kernel_missing_default_packages','model_calls_attempted':0}),flush=True)
        print(json.dumps({'event':'result','outcome':'kernel_default_packages_preflight_failed','model_calls_attempted':0}),flush=True);return 44
    if not docker_preflight(env):
        print(json.dumps({'event':'guest_bootstrap_failed','stage':'guest_docker','reason':'guest_docker_startup_failed','model_calls_attempted':0}),flush=True);return 44
    raw=(json.dumps({'key':envelope['key'],'request':envelope['request'],'token':envelope['token'],'model':envelope.get('model'),'provider_hosts':envelope.get('provider_hosts',{})})+'\n').encode()
    child=subprocess.Popen(['/usr/bin/python3','-I','/opt/worker/guest.py'],env=env,stdin=subprocess.PIPE)
    child.stdin.write(raw);child.stdin.flush()
    def pump():
        try:
            while True:
                chunk=os.read(sys.stdin.fileno(),65536)
                if not chunk:break
                child.stdin.write(chunk);child.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:
            try:child.stdin.close()
            except OSError:pass
    threading.Thread(target=pump,daemon=True).start()
    return child.wait()



if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        print(json.dumps({'event':'result','outcome':'setup_uncertain_no_retry','model_calls_attempted':0}));sys.exit(2)
