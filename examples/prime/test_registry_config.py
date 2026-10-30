import test_support
import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
from test_project_cli import w
class RegistryConfig(unittest.TestCase):
 def test_operator_discovery_and_explicit_override(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)):
   d=Path(td)/'.config/nomad-boxlite-prime';d.mkdir(parents=True);p=d/'operator.json';p.write_text(json.dumps({'registry_origin':'https://registry.example/','nomad_address':'https://nomad.example'}));p.chmod(0o600)
   self.assertEqual(w.configured_registry_origin(),'https://registry.example');self.assertEqual(w.configured_registry_origin('https://other.example'),'https://other.example')
   self.assertEqual(w.main(['registries'])['registries'],['https://registry.example'])
 def test_missing_and_secret_bearing_origins_fail_before_network(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)):
   with self.assertRaisesRegex(ValueError,'registry_not_configured'):w.configured_registry_origin()
  for v in ['http://registry.example','https://user:secret@registry.example','https://registry.example/path','https://registry.example?token=secret']:
   with self.assertRaises(ValueError):w.configured_registry_origin(v)
 def test_tools_unknown_or_explicit_not_guessed_from_name(self):
  self.assertEqual(w.declared_tool_metadata({'org.opencontainers.image.title':'Go Node Playwright libghostty'})['state'],'unknown')
  v=w.declared_tool_metadata({'io.boxlite.tool.go.version':'1.26.5','io.boxlite.tool.node.version':'24.22.1','io.boxlite.tool.secret.version':'https://secret.example/token','unknown':'PRIVATE'})
  self.assertEqual(v['tools'],{'go':'1.26.5','node':'24.22.1'});self.assertFalse(v['runtime_verified']);self.assertNotIn('PRIVATE',json.dumps(v));self.assertNotIn('secret.example',json.dumps(v))
 def test_images_default_uses_operator_origin_without_nomad_or_credentials(self):
  with patch.object(w,'configured_registry_origin',return_value='https://registry.example'),patch.object(w,'registry_images',return_value={'images':[]}) as discover,patch.object(w,'Nomad',side_effect=AssertionError('No Nomad credential needed')):
   out=w.main(['images','--workers-only','--architecture','amd64']);self.assertEqual(out['registry_source'],'operator_config');discover.assert_called_once_with('https://registry.example',None,True,'amd64')
if __name__=='__main__':unittest.main()
