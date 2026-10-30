import test_support
import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('guest_runtime_diagnostics',Path(__file__).parent/'guest/guest.py');guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)
class RuntimeDiagnostics(unittest.TestCase):
 def test_auth_cause_saved_without_payload_or_secret(self):
  with tempfile.TemporaryDirectory() as d,patch.object(guest,'WORK',Path(d)):
   secret='synthetic-sensitive-value';private='private-prompt-thought-tool-code'
   record=guest.runtime_diagnostic('pa-daemon: acp turn failed: 401 Unauthorized '+secret+' '+private,'prime_acp',[secret],-32603)
   text=(Path(d)/'.nomad-diagnostics/runtime.jsonl').read_text();self.assertNotIn(secret,text);self.assertNotIn(private,text);self.assertIn('authentication_failed',record['categories']);self.assertEqual(record['rpc_code'],-32603);self.assertEqual(record['http_status_candidates'],[401]);self.assertEqual((Path(d)/'.nomad-diagnostics/runtime.jsonl').stat().st_mode&0o777,0o600)
 def test_unknown_stderr_is_fingerprint_only(self):
  with tempfile.TemporaryDirectory() as d,patch.object(guest,'WORK',Path(d)):
   guest.runtime_diagnostic('arbitrary private payload','acp_server')
   x=json.loads((Path(d)/'.nomad-diagnostics/runtime.jsonl').read_text());self.assertEqual(x['categories'],['unclassified_stderr']);self.assertTrue(x['payload_omitted']);self.assertNotIn('arbitrary',json.dumps(x))
 def test_missing_key_fails_before_starting_acp(self):
  with patch.dict(os.environ,{},clear=True),patch.object(guest.sys,'argv',['guest.py','--acp-proxy',guest.PRIME]):
   with self.assertRaisesRegex(RuntimeError,'credential environment'):guest.acp_proxy()
 def test_log_is_bounded(self):
  with tempfile.TemporaryDirectory() as d,patch.object(guest,'WORK',Path(d)):
   root=Path(d)/'.nomad-diagnostics';root.mkdir();p=root/'runtime.jsonl';p.write_bytes(b'x'*262144);before=p.stat().st_size
   guest.runtime_diagnostic('401 fixture','prime_acp');self.assertEqual(p.stat().st_size,before)
