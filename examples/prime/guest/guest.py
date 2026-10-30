"""Guest persistent lifecycle. Prime owns one daemon; Node retains one ACP handle."""
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time
import threading
import hashlib
import re
import collections
import ipaddress

AGENT='/root/.prime/nomad-persistent'
SOCKET='/tmp/nomad-prime.sock'
PRIME='/usr/local/bin/prime-agent'
ACPX='/usr/local/bin/acpx'
WORK=Path('/workspace/repo')
STAGE='startup'
def stage_event(event, **fields):
    print(json.dumps({'event':event,'layer':'guest_bootstrap','stage':STAGE,'timestamp':time.time(),**fields}),flush=True)


def read_line(fd,limit):
    out=bytearray()
    while len(out)<limit:
        b=os.read(fd,1)
        if not b or b==b'\n':return bytes(out)
        out.extend(b)
    raise ValueError('Guest startup line limit')


def env():
    return {'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/root','PI_OFFLINE':'1',
            'PRIME_AGENT_CODING_AGENT_DIR':AGENT,'PRIME_AGENT_DAEMON_SOCKET':SOCKET,
            'PRIME_AGENT_KERNEL_PYTHON':'/opt/worker/prime/prime-agent-runtime/.venv/bin/python',
            'GIT_TERMINAL_PROMPT':'0'}


def bounded_command(argv, timeout=30):
    started=time.monotonic()
    stage_event('stage_started',deadline_seconds=timeout,attempt=1)
    p=subprocess.Popen(argv,cwd=WORK,env=env(),stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    out=bytearray();sel=selectors.DefaultSelector();sel.register(p.stdout,selectors.EVENT_READ)
    deadline=time.monotonic()+timeout
    try:
        while sel.get_map():
            if time.monotonic()>deadline:raise TimeoutError('Guest command timeout')
            for k,_ in sel.select(.1):
                b=os.read(k.fileobj.fileno(),4096)
                if not b:sel.unregister(k.fileobj);continue
                out.extend(b)
                if len(out)>16384:raise ValueError('Guest command output limit')
        code=p.wait(timeout=1)
        if code and out:sys.stderr.write(out.decode(errors='replace'));sys.stderr.flush()
        text=out.decode(errors='replace').lower()
        classes=[name for name,patterns in {'tls':['certificate','ssl error','tls'],'dns':['could not resolve','name resolution'],'authentication':['authentication failed','could not read username','permission denied'],'network':['connection refused','network is unreachable','failed to connect'],'not_found':['repository not found'],'disk':['no space left']}.items() if any(pattern in text for pattern in patterns)]
        stage_event('stage_finished',deadline_seconds=timeout,exit_code=code,signal=-code if code<0 else None,error_classes=classes,elapsed_seconds=round(time.monotonic()-started,3))
        return code,out.decode(errors='replace')
    except Exception as exc:
        # Never echo arbitrary subprocess output/URLs/private filenames.
        stage_event('stage_failed',exception_type=type(exc).__name__,reason='deadline_exceeded' if isinstance(exc,TimeoutError) else 'command_failed',deadline_seconds=timeout,elapsed_seconds=round(time.monotonic()-started,3))
        raise
    finally:
        sel.close()
        if p.poll() is None:
            os.killpg(p.pid,signal.SIGKILL);p.wait()


def prepare_request(d):
    global STAGE
    WORK.mkdir(parents=True,exist_ok=True)
    if d.get('repo'):
        # Public URL/full commit were checked by the common contract on both hosts.
        STAGE='repo_clone'
        code,_=bounded_command(['git','clone','--no-checkout','--',d['repo']['url'],str(WORK)],120)
        if code:raise RuntimeError('guest_repo_clone_failed')
        STAGE='repo_checkout'
        code,_=bounded_command(['git','checkout','--detach',d['repo']['commit']],60)
        if code:raise RuntimeError('guest_repo_checkout_failed')
    for name,text in d.get('files',{}).items():
        p=(WORK/name).resolve();p.relative_to(WORK.resolve())
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    outputs=[]
    for cmd in d.get('commands',[]):
        STAGE='initial_command'
        code,out=bounded_command(cmd)
        outputs.append({'exit_code':code,'output':out})
        if code:raise RuntimeError('guest_command_failed')
    return outputs


DIAGNOSTIC_LOCK=threading.Lock()
def runtime_diagnostic(text, source, secrets=(), rpc_code=None):
    # Retain categorical stderr/error evidence, never prompts/thoughts/tool args.
    value=str(text)[:65536]
    for secret in secrets:
        if secret:value=value.replace(secret,'[REDACTED]').replace(json.dumps(secret)[1:-1],'[REDACTED]')
    lower=value.lower();categories=[]
    patterns={'authentication_failed':r'unauthorized|invalid.api.key|authentication.failed|\b401\b',
              'forbidden':r'forbidden|\b403\b','rate_limited':r'rate.limit|\b429\b',
              'dns_failure':r'dns|name or service not known|failed to lookup|resolve host',
              'tls_failure':r'certificate|tls|ssl','connection_failed':r'connection refused|connection error|connect error|network is unreachable',
              'model_unavailable':r'model not found|unknown model|no available|model.*not.*supported',
              'missing_dependency':r'modulenotfounderror|no module named|kernel.*missing',
              'kernel_failure':r'kernel.*(?:fail|error)|python.*(?:fail|error)',
              'timeout':r'timed out|timeout','context_limit':r'context.*(?:limit|length)|max.tokens',
              'runtime_error':r'internal error|turn failed|runtimeerror|exception|error'}
    for category,pattern in patterns.items():
        if re.search(pattern,lower):categories.append(category)
    record={'event':'runtime_diagnostic','source':source,'timestamp':time.time(),
            'categories':categories or ['unclassified_stderr'],
            'redacted_sha256':hashlib.sha256(value.encode()).hexdigest(),
            'bytes_observed':len(str(text).encode()),'payload_omitted':True}
    if isinstance(rpc_code,int):record['rpc_code']=rpc_code
    statuses=sorted(set(int(x) for x in re.findall(r'\b(400|401|403|404|408|413|422|429|500|502|503|504)\b',value)))
    if statuses:record['http_status_candidates']=statuses
    root=WORK/'.nomad-diagnostics';root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or not root.is_dir():raise ValueError('Invalid diagnostic directory')
    path=root/'runtime.jsonl'
    with DIAGNOSTIC_LOCK:
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            if os.fstat(fd).st_size<262144:os.write(fd,(json.dumps(record)+'\n').encode());os.fsync(fd)
        finally:os.close(fd)
    return record

def capture_runtime_stderr(process, source, secrets):
    def pump():
        while True:
            line=process.stderr.readline(65537)
            if not line:return
            runtime_diagnostic(line.decode(errors='replace'),source,secrets)
    threading.Thread(target=pump,daemon=True).start()

def acp_proxy():
    # The trusted ACP client must resolve the env-name models.json credential.
    # No credentials in argv/files; stdout remains the unchanged ACP wire.
    args=sys.argv[2:]
    if not args or args[0]!=PRIME:raise ValueError('Invalid fixed ACP command')
    secret=os.environ.get('LLM_API_KEY','')
    if not secret:raise RuntimeError('Missing trusted ACP credential environment')
    p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=WORK)
    def stop_child(*_):
        if p.poll() is None:p.terminate()
    signal.signal(signal.SIGTERM,stop_child);signal.signal(signal.SIGINT,stop_child)
    capture_runtime_stderr(p,'prime_acp',[secret])
    def stdin():
        try:
            while True:
                chunk=os.read(sys.stdin.fileno(),65536)
                if not chunk:break
                p.stdin.write(chunk);p.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:
            try:p.stdin.close()
            except OSError:pass
    threading.Thread(target=stdin,daemon=True).start()
    while True:
        line=p.stdout.readline(131073)
        if not line:break
        sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
        if len(line)<=131072 and line.endswith(b'\n'):
            try:o=json.loads(line)
            except ValueError:continue
            error=o.get('error') if isinstance(o,dict) else None
            if isinstance(error,dict):
                details=(error.get('data') or {}).get('details','') if isinstance(error.get('data'),dict) else ''
                runtime_diagnostic(str(error.get('message',''))+' '+str(details),'acp_error',[secret],error.get('code'))
    return p.wait()


def apply_provider_hosts(aliases, path=Path('/etc/hosts')):
    if not isinstance(aliases,dict) or len(aliases)>1:raise ValueError('Invalid operator provider aliases')
    additions=[]
    for host,addresses in aliases.items():
        if not isinstance(host,str) or not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}',host) or not isinstance(addresses,list) or not 1<=len(addresses)<=16:raise ValueError('Invalid operator provider alias')
        for value in addresses:
            ip=ipaddress.ip_address(value)
            if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:raise ValueError('Invalid provider private address')
            additions.append(str(ip)+' '+host+' # boxlite-prime-provider')
    if not additions:return
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    with os.fdopen(fd,'r+') as stream:
        old=stream.read(65537)
        if len(old)>65536:raise ValueError('Provider hosts file too large')
        lines=[line for line in old.splitlines() if '# boxlite-prime-provider' not in line]
        stream.seek(0);stream.write('\n'.join(lines+additions)+'\n');stream.truncate();stream.flush();os.fsync(stream.fileno())


def main():
    global STAGE
    d=json.loads(read_line(sys.stdin.fileno(),1048576));key=d['key'];request=d['request'];token=d['token']
    if not isinstance(key,str) or not key or len(key)>8192 or not key.isascii() or any(ord(x)<33 or ord(x)>126 for x in key):
        raise ValueError('Invalid key input')
    STAGE='child_config'
    apply_provider_hosts(d.get('provider_hosts',{}))
    prepare_request(request)
    daemon=None;server=None
    def stop(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        STAGE='prime_daemon_start'
        stage_event('stage_started',deadline_seconds=30)
        daemon=subprocess.Popen([PRIME,'--mode','daemon','--daemon-socket',SOCKET],
            env={**env(),'LLM_API_KEY':key},cwd=WORK,stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
        capture_runtime_stderr(daemon,'prime_daemon',[key,token])
        deadline=time.monotonic()+30
        while not Path(SOCKET).exists():
            if daemon.poll() is not None or time.monotonic()>deadline:raise RuntimeError('daemon_not_ready')
            time.sleep(.05)
        stage_event('stage_finished')
        STAGE='acp_server_start'
        server=subprocess.Popen(['/usr/local/bin/node','/opt/worker/acpx/server.mjs'],env={**env(),'LLM_API_KEY':key},cwd=WORK,
            stdin=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        capture_runtime_stderr(server,'acp_server',[key,token])
        server.stdin.write((json.dumps({'session_id':request['session_id'],'token':token,'model':d.get('model')})+'\n').encode())
        server.stdin.flush()
        def pump():
            try:
                while True:
                    chunk=os.read(sys.stdin.fileno(),65536)
                    if not chunk:break
                    server.stdin.write(chunk);server.stdin.flush()
            except (BrokenPipeError,OSError):pass
            finally:
                try:server.stdin.close()
                except OSError:pass
        threading.Thread(target=pump,daemon=True).start()
        code=server.wait()
        print(json.dumps({'event':'result','session_id':request['session_id'],
            'outcome':'session_closed' if code==0 else 'session_failed_no_retry'}),flush=True)
        return code
    finally:
        for p in (server,daemon):
            if p is not None and p.poll() is None:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(5)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()

if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--acp-proxy':
    sys.exit(acp_proxy())

if __name__=='__main__':
    try:code=main()
    except (Exception,KeyboardInterrupt) as exc:
        __import__('traceback').print_exc(file=sys.stderr)
        stage_event('guest_bootstrap_failed',exception_type=type(exc).__name__,reason='interrupted' if isinstance(exc,KeyboardInterrupt) else 'bootstrap_failed')
        print(json.dumps({'event':'result','outcome':'session_stopped_or_failed_no_retry'}),flush=True);code=2
    sys.exit(code)
