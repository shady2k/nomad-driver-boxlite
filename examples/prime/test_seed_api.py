import unittest,tempfile,json,io,urllib.error,sys
from pathlib import Path
from unittest.mock import patch,Mock
import dispatcher_auth as a
class Tests(unittest.TestCase):
 def setup(self,home):
  c=home/'.config/nomad-boxlite-prime';c.mkdir(parents=True)
  for p,d in [(c/'environment.json',{'VAULT_ADDR':'https://bao.example'}),(c/'operator.json',{'seed_mount':'secret','seed_path':'workers/dispatcher','bao_binary':'/old/unused/bao'})]:p.write_text(json.dumps(d));p.chmod(0o600)
  (home/'.vault-token').write_text('SYNTHETIC_TOKEN');(home/'.vault-token').chmod(0o600)
 def test_exact_api_no_preflight(self):
  with tempfile.TemporaryDirectory() as t,patch.object(a.Path,'home',return_value=Path(t)),patch.dict(a.os.environ,{},clear=True):
   self.setup(Path(t));response=io.BytesIO(json.dumps({'data':{'data':{'BOXLITE_DISPATCHER_SEED':'a'*64}}}).encode());opener=Mock();opener.open.return_value=response
   with patch('urllib.request.build_opener',return_value=opener):self.assertEqual(a.fetch_seed(),bytes.fromhex('a'*64))
   req=opener.open.call_args.args[0];self.assertEqual(req.full_url,'https://bao.example/v1/secret/data/workers/dispatcher');self.assertEqual(opener.open.call_count,1);self.assertNotIn('SYNTHETIC_TOKEN',req.full_url)
 def test_http_failures_no_secret_or_body(self):
  for code in [403,404,500]:
   with tempfile.TemporaryDirectory() as t,patch.object(a.Path,'home',return_value=Path(t)),patch.dict(a.os.environ,{},clear=True):
    self.setup(Path(t));opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://bao.example',code,'body-SYNTHETIC_TOKEN',{},None)
    with patch('urllib.request.build_opener',return_value=opener),self.assertRaises(a.SeedUnavailable) as error:a.fetch_seed()
    self.assertIn(str(code),str(error.exception));self.assertNotIn('SYNTHETIC_TOKEN',str(error.exception));self.assertEqual(opener.open.call_count,1)
 def test_missing_field_and_oversize(self):
  for raw in [b'{}',b'x'*65537]:
   with tempfile.TemporaryDirectory() as t,patch.object(a.Path,'home',return_value=Path(t)),patch.dict(a.os.environ,{},clear=True):
    self.setup(Path(t));opener=Mock();opener.open.return_value=io.BytesIO(raw)
    with patch('urllib.request.build_opener',return_value=opener),self.assertRaises(a.SeedUnavailable):a.fetch_seed()
 def test_unsafe_selector_fails_before_network(self):
  with tempfile.TemporaryDirectory() as t,patch.object(a.Path,'home',return_value=Path(t)),patch.dict(a.os.environ,{},clear=True):
   self.setup(Path(t));p=Path(t)/'.config/nomad-boxlite-prime/operator.json';d=json.loads(p.read_text());d['seed_path']='../another';p.write_text(json.dumps(d))
   with patch('urllib.request.build_opener') as open,self.assertRaises(a.SeedUnavailable):a.fetch_seed()
   open.assert_not_called()
