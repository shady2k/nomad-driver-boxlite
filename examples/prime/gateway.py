"""Fixed source-IP gate. No host command or caller-selected upstream path."""
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from urllib.parse import urlsplit,parse_qs

LAN_IP=None
SOURCE_IP=None
PORT=None

def protected_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st=os.fstat(f.fileno())
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):
            raise ValueError('Expected own regular credential/config file mode0600')
        return json.load(f)

def allowed(source,method,path,authorization,token,session_id,header_session):
    if not SOURCE_IP or source!=SOURCE_IP:return False
    if not hmac.compare_digest(authorization or '', 'Bearer '+token):return False
    if header_session!=session_id:return False
    return ((method=='GET' and (path in ('/status','/events') or re.fullmatch(r'/result/[0-9a-f-]{36}',path))) or
            (method=='POST' and path in ('/prompt','/cancel','/close','/artifacts','/artifact-transfer','/answer','/exchange')))

class SingleVM:
    def __init__(self,path):self.path=Path(path);self.fd=None
    def __enter__(self):
        self.fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        st=os.fstat(self.fd)
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:
            os.close(self.fd);self.fd=None;raise ValueError('Unsafe VM lock')
        try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:os.close(self.fd);self.fd=None;raise
        return self
    def __exit__(self,*_):
        if self.fd is not None:os.close(self.fd);self.fd=None

class Gateway:
    def __init__(self,token,session_id,call,port=None,journal=None):
        from profiles import port as approved_port
        port=approved_port(port)
        self.port=int(port);self.journal=journal
        self.token,self.session_id,self.call=token,session_id,call;self.server=None;self.thread=None
    def __enter__(self):
        gate=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*_):pass
            def do_GET(self):self.forward()
            def do_POST(self):self.forward()
            def diagnostic(self,event,phase,error=None):
                # Fixed metadata only: never URLs/queries, request bodies,
                # authorization, model text or exception messages.
                record={'event':event,'session_id':gate.session_id,'phase':phase,'time':time.time()}
                if error is not None:record['error_type']=type(error).__name__
                print(json.dumps(record),file=sys.stderr,flush=True)
            def forward(self):
                response_started=False
                phase='request_validation'
                self.connection.settimeout(130)
                parsed=urlsplit(self.path)
                if not allowed(self.client_address[0],self.command,parsed.path,
                        self.headers.get('Authorization'),gate.token,gate.session_id,self.headers.get('X-Session-ID')):
                    try:self.send_error(403)
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError) as e:self.diagnostic('gateway_client_disconnected','authorization_response',e)
                    return
                try:
                    if parsed.path=='/events':
                        if gate.journal is None:raise ValueError('Events unavailable on legacy session')
                        q=parse_qs(parsed.query);after=int(q.get('after',['0'])[0]);seconds=min(604800,max(1,int(q.get('deadline',['300'])[0])))
                        if after<0:raise ValueError('cursor')
                        phase='downstream_events';response_started=True
                        self.send_response(200);self.send_header('Content-Type','application/x-ndjson');self.send_header('Cache-Control','no-store');self.end_headers()
                        for e in gate.journal.stream(after,time.monotonic()+seconds):
                            self.wfile.write((json.dumps(e)+'\n').encode());self.wfile.flush()
                        return
                    if self.headers.get('Transfer-Encoding'):raise ValueError('No chunked requests')
                    n=int(self.headers.get('Content-Length','0'))
                    if not 0<=n<=131072:raise ValueError('Body limit')
                    body=self.rfile.read(n)
                    if len(body)!=n:raise ValueError('Short request')
                    phase='upstream_call'
                    status,raw=gate.call(self.path,self.command,body)
                    if len(raw)>131072:raise ValueError('Response limit')
                    phase='downstream_response';response_started=True
                    self.send_response(status);self.send_header('Content-Type','application/json')
                    self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
                except Exception as e:
                    disconnected=isinstance(e,(BrokenPipeError,ConnectionResetError,ConnectionAbortedError))
                    if disconnected and phase!='upstream_call':
                        # The request may have executed: never replay it here.
                        self.diagnostic('gateway_client_disconnected',phase,e)
                        self.close_connection=True
                        return
                    self.diagnostic('gateway_request_error',phase,e)
                    if response_started:
                        # Headers/body may already be on the wire. A second
                        # HTTP status would corrupt the response/NDJSON stream.
                        self.close_connection=True
                        return
                    try:self.send_error(503,'Unavailable; inspect status before retry')
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError) as disconnect:
                        self.diagnostic('gateway_client_disconnected','error_response',disconnect)
                        self.close_connection=True
        self.server=ThreadingHTTPServer((LAN_IP,self.port),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return self
    def __exit__(self,*_):
        if self.journal:self.journal.close()
        if self.server:self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(2)
