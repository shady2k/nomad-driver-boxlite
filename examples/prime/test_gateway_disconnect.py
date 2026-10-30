import contextlib,importlib.util,io,json,sys,types,unittest
from unittest.mock import Mock,patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('gateway_tested',Path(__file__).with_name('gateway.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
class Server:
 def __init__(self,address,handler):self.handler=handler
 def serve_forever(self):pass
 def shutdown(self):pass
 def server_close(self):pass
class Tests(unittest.TestCase):
 def run_request(self,call,write_error=None,send_error_error=None,path='/status',journal=None):
  g.LAN_IP=g.SOURCE_IP='127.0.0.1'
  err=io.StringIO()
  with patch.dict(sys.modules,{'profiles':types.SimpleNamespace(port=lambda v:v)}),patch.object(g,'ThreadingHTTPServer',Server),contextlib.redirect_stderr(err):
   with g.Gateway('SECRET_TOKEN','test-session',call,port=1,journal=journal) as gate:
    h=gate.server.handler.__new__(gate.server.handler)
    h.path=path;h.command='GET';h.client_address=('127.0.0.1',123)
    h.headers={'Authorization':'Bearer SECRET_TOKEN','X-Session-ID':'test-session'}
    h.connection=Mock();h.rfile=io.BytesIO();h.wfile=Mock()
    h.wfile.write.side_effect=write_error
    h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock();h.send_error=Mock(side_effect=send_error_error)
    h.forward()
    return h,err.getvalue()
 def test_downstream_disconnect_no_503_or_replay(self):
  call=Mock(return_value=(200,b'{}'))
  h,logs=self.run_request(call,write_error=BrokenPipeError('SECRET_TOKEN'))
  self.assertEqual(call.call_count,1);h.send_error.assert_not_called()
  self.assertIn('gateway_client_disconnected',logs);self.assertNotIn('SECRET_TOKEN',logs)
 def test_upstream_error_once_503(self):
  call=Mock(side_effect=RuntimeError('https://x/?token=SECRET_TOKEN'))
  h,logs=self.run_request(call)
  self.assertEqual(call.call_count,1);h.send_error.assert_called_once()
  self.assertIn('upstream_call',logs);self.assertNotIn('SECRET_TOKEN',logs)
 def test_upstream_socket_error_is_not_client_disconnect(self):
  h,logs=self.run_request(Mock(side_effect=BrokenPipeError('secret')))
  h.send_error.assert_called_once();self.assertIn('gateway_request_error',logs)
 def test_disconnected_error_response_is_suppressed(self):
  h,logs=self.run_request(Mock(side_effect=RuntimeError()),send_error_error=BrokenPipeError())
  h.send_error.assert_called_once();self.assertIn('error_response',logs)
 def test_stream_error_after_headers_no_second_response(self):
  j=Mock();j.stream.side_effect=RuntimeError('SECRET_TOKEN')
  h,logs=self.run_request(Mock(),path='/events',journal=j)
  h.send_error.assert_not_called();h.send_response.assert_called_once_with(200)
  self.assertIn('gateway_request_error',logs);self.assertNotIn('SECRET_TOKEN',logs)
 def test_success(self):
  h,logs=self.run_request(Mock(return_value=(200,b'{}')))
  h.send_error.assert_not_called();h.wfile.write.assert_called_once_with(b'{}');self.assertEqual(logs,'')
if __name__=='__main__':unittest.main()
