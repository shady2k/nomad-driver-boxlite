import unittest,json,threading,urllib.request,http.server
from model_policy import session_models
class TestHeader(unittest.TestCase):
 def test_conversation_binding_and_no_mutation(self):
  policy={'provider':'fixture','default_model':'model','allowed_models':['model']};models={'providers':{'fixture':{'apiKey':'LLM_API_KEY','headers':{'X-Opencode-Session':'stale','X-Other':'preserved'}}}}
  a='11111111-1111-4111-8111-111111111111';b='22222222-2222-4222-8222-222222222222'
  self.assertEqual(session_models(models,policy,a),session_models(models,policy,a));self.assertNotEqual(session_models(models,policy,a),session_models(models,policy,b));self.assertEqual(models['providers']['fixture']['headers']['X-Opencode-Session'],'stale')
  with self.assertRaises(ValueError):session_models(models,policy,'invalid\r\nheader')
 def test_local_forwarding_stream_fixture(self):
  policy={'provider':'fixture','default_model':'model','allowed_models':['model']};sid='11111111-1111-4111-8111-111111111111';cfg=session_models({'providers':{'fixture':{}}},policy,sid);seen=[]
  class Upstream(http.server.BaseHTTPRequestHandler):
   def do_POST(self):
    seen.append(self.headers.get('x-opencode-session'));n=int(self.headers.get('Content-Length','0'));self.rfile.read(n)
    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(b'data: {"choices":[{"delta":{"content":"fixture"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
   def log_message(self,*a):pass
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   for _ in range(2):
    req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port),data=b'{}',headers=cfg['providers']['fixture']['headers']);body=urllib.request.urlopen(req).read();self.assertTrue(body.endswith(b'data: [DONE]\n\n'))
   self.assertEqual(seen,[sid,sid])
  finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()

class TestTerminalProjection(unittest.TestCase):
 def test_untrusted_metadata_cannot_claim_task_success_or_expose_payload(self):
  import observability
  v=observability.terminal_metadata({'observation':'terminal','stop_reason':'end_turn','task_completion':'verified','automatic_retry':True,'http_status':True,'attempt_count':-1,'upstream_request_id_sha256':'SECRET','error_code':'PRIVATE','payload':'PRIVATE'})
  self.assertEqual(v['task_completion'],'unverified');self.assertFalse(v['automatic_retry']);self.assertIsNone(v['http_status']);self.assertIsNone(v['attempt_count']);self.assertNotIn('PRIVATE',json.dumps(v));self.assertNotIn('SECRET',json.dumps(v))
