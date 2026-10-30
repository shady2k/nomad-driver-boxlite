import contextlib,http.client,importlib.util,io,json,socket,struct,sys,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('tested_gateway',Path(__file__).with_name('gateway.py'));g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
class Journal:
 def __init__(self,fail=False):self.release=threading.Event();self.fail=fail
 def stream(self,*_):
  yield {'event':'synthetic_first'}
  self.release.wait(3)
  if self.fail:raise RuntimeError('SECRET_PAYLOAD')
  for i in range(8):yield {'event':'synthetic_next','sequence':i}
 def close(self):self.release.set()
class Tests(unittest.TestCase):
 def test_real_http_disconnect_and_errors(self):
  g.LAN_IP=g.SOURCE_IP='127.0.0.1';journal=Journal();logs=io.StringIO();calls=[]
  def upstream(path,method,body):
   calls.append(path)
   if path=='/result/00000000-0000-0000-0000-000000000000':raise RuntimeError('SECRET_PAYLOAD')
   return 200,b'{"state":"ready"}'
  with patch.dict(sys.modules,{'profiles':types.SimpleNamespace(port=lambda x:x)}),contextlib.redirect_stderr(logs):
   with g.Gateway('SYNTHETIC_TOKEN','synthetic-session',upstream,port=0,journal=journal) as gate:
    port=gate.server.server_address[1];headers={'Authorization':'Bearer SYNTHETIC_TOKEN','X-Session-ID':'synthetic-session'}
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);c.request('GET','/status',headers=headers);r=c.getresponse();self.assertEqual(r.status,200);self.assertEqual(json.loads(r.read())['state'],'ready');c.close()
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);c.request('GET','/result/00000000-0000-0000-0000-000000000000',headers=headers);r=c.getresponse();self.assertEqual(r.status,503);r.read();c.close()
    s=socket.create_connection(('127.0.0.1',port),timeout=3);s.sendall(b'GET /events HTTP/1.0\r\nAuthorization: Bearer SYNTHETIC_TOKEN\r\nX-Session-ID: synthetic-session\r\n\r\n');data=b''
    while b'synthetic_first' not in data:data+=s.recv(4096)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));s.close();journal.release.set()
    end=time.monotonic()+3
    while 'gateway_client_disconnected' not in logs.getvalue() and time.monotonic()<end:time.sleep(.01)
    self.assertIn('gateway_client_disconnected',logs.getvalue())
    journal.fail=True
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);c.request('GET','/events',headers=headers);r=c.getresponse();self.assertEqual(r.status,200);body=r.read();self.assertNotIn(b'503',body);self.assertIn(b'synthetic_first',body);c.close()
  self.assertEqual(calls,['/status','/result/00000000-0000-0000-0000-000000000000'])
  self.assertNotIn('Traceback',logs.getvalue());self.assertNotIn('SECRET_PAYLOAD',logs.getvalue());self.assertNotIn('SYNTHETIC_TOKEN',logs.getvalue())
  self.assertIn('gateway_request_error',logs.getvalue())
if __name__=='__main__':unittest.main()
