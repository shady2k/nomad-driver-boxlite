import test_support
import json,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
import dispatcher_auth as auth,gateway
class OperatorConfig(unittest.TestCase):
 def test_secret_selector_is_operator_owned_and_never_payload(self):
  with tempfile.TemporaryDirectory() as td:
   h=Path(td);c=h/'.config/nomad-boxlite-prime';c.mkdir(parents=True)
   for p,value in [(c/'environment.json',{'VAULT_ADDR':'https://bao.example'}),(c/'operator.json',{'seed_mount':'secret','seed_path':'workers/dispatcher','bao_binary':'/absolute/bao'})]:p.write_text(json.dumps(value));p.chmod(0o600)
   token=h/'.vault-token';token.write_text('fixture-only-token');token.chmod(0o600)
   import io,urllib.request
   from unittest.mock import Mock
   opener=Mock();opener.open.return_value=io.BytesIO(json.dumps({'data':{'data':{'BOXLITE_DISPATCHER_SEED':'a'*64}}}).encode())
   with patch.object(auth.Path,'home',return_value=h),patch.dict(auth.os.environ,{},clear=True),patch('urllib.request.build_opener',return_value=opener):
    self.assertEqual(auth.fetch_seed(),bytes.fromhex('a'*64))
   request=opener.open.call_args.args[0];self.assertEqual(request.full_url,'https://bao.example/v1/secret/data/workers/dispatcher');self.assertNotIn('fixture-only-token',request.full_url);self.assertEqual(opener.open.call_count,1)

 def test_unconfigured_gateway_is_closed(self):
  with patch.object(gateway,'SOURCE_IP',None):self.assertFalse(gateway.allowed(None,'GET','/status','Bearer x','x','s','s'))
 def test_gateway_configured_source_binding_remains_required(self):
  with patch.object(gateway,'SOURCE_IP','192.0.2.10'):
   self.assertTrue(gateway.allowed('192.0.2.10','GET','/status','Bearer x','x','s','s'));self.assertFalse(gateway.allowed('192.0.2.11','GET','/status','Bearer x','x','s','s'))
