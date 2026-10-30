import test_support
import unittest,copy,json
import model_policy,universal
from test_universal import req,catalog
class ModelPolicy(unittest.TestCase):
 def policy(self):return {'provider':'operator-provider','default_model':'model-default','allowed_models':['model-default']}
 def test_default_and_explicit(self):
  p=self.policy();self.assertEqual(model_policy.select_model({},p),'model-default');self.assertEqual(model_policy.select_model({'model':'model-default'},p),'model-default')
 def test_unknown_and_provider_url_not_accepted(self):
  for model in ['other-model','https://provider.example','bad value']:
   with self.assertRaises(ValueError):model_policy.select_model({'model':model},self.policy())
  for allowed in [[],[{}],['model-default','model-default']]:
   p=self.policy();p['allowed_models']=allowed
   with self.assertRaises(ValueError):model_policy.select_model({},p)
 def test_optional_model_is_bound_without_mutating_legacy_request(self):
  d=universal.validate(req());self.assertNotIn('model',d);original=universal.identity(d);d['model']='model-default';d=universal.validate(d);j=universal.build_job(d,catalog());self.assertEqual(json.loads(j['TaskGroups'][0]['Tasks'][0]['Config']['worker_request'])['model'],'model-default');self.assertNotEqual(original,universal.identity(d));self.assertNotIn('provider',d)

class CompanionModelAdmission(unittest.TestCase):
 def test_actual_companion_admission_binds_model_before_create(self):
  import companion,tempfile
  from pathlib import Path
  from unittest.mock import patch
  policy={'provider':'operator-provider','default_model':'model-default','allowed_models':['model-default']}
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/'guest').mkdir()
   for path,data in [(root/'node.json',{'lan_ip':'192.0.2.20'}),(root/'guest/session-policy.json',policy)]:path.write_text(json.dumps(data));path.chmod(0o600)
   d=req();d['model']='model-default';e={'request':json.dumps(d),'namespace':'boxlite-prime','task_name':'vm','job_id':universal.parent(d),'node_id':d['node_id'],'logical_id':d['session_id'],'image':d['image'],'cpus':d['cpus'],'memory_mib':d['memory_mib'],'disk_gib':d['disk_gib'],'port':20000,'host':'192.0.2.20','allocation_id':'11111111-1111-4111-8111-111111111111'}
   with patch.object(companion,'ROOT',root),patch.object(companion,'HERE',root):
    validated,identity=companion.validate(e);self.assertEqual(validated['model'],'model-default');self.assertEqual(identity,universal.identity(d))
    d['model']='unapproved-model';e['request']=json.dumps(d)
    with self.assertRaisesRegex(ValueError,'operator allowed'):companion.validate(e)
