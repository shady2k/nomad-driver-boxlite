import test_support
import unittest,importlib.util,sys,tempfile,contextlib,io
from pathlib import Path
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parent/'claude-plugin/scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('project_workerctl',SCRIPTS/'workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
from test_universal import catalog,NODE
class API:
 def __init__(self):self.calls=[];self.job=None
 def call(self,path,method='GET',body=None):
  self.calls.append((path,method,body))
  if path=='/v1/nodes?resources=true':return [{'ID':NODE}]
  if path=='/v1/node/'+NODE:return {'ID':NODE,'Datacenter':'dc1','NodePool':'default','Attributes':{'cpu.arch':'arm64','driver.boxlite.api':'v1'}}
  if path=='/v1/jobs':self.job=body['Job'];return {'EvalID':'fixture-evaluation'}
  if path.startswith('/v1/job/'):return self.job
  raise AssertionError(path)
class Tests(unittest.TestCase):
 def test_request_project_to_jobspec_receipt_and_status(self):
  api=API();policy={'provider':'fixture','default_model':'example-model','allowed_models':['example-model']}
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)),patch.object(w,'policy_catalog',return_value=catalog()),patch.object(w.auth,'protected_json',return_value=policy),patch.object(w.auth,'access',return_value={'token':'synthetic'}),contextlib.redirect_stdout(io.StringIO()):
   r=w.main(['request','--architecture','arm64','--project','Project Alpha','--image','registry.example/image@sha256:'+'a'*64,'--cpus','1','--memory-mib','512'],api=api)
   self.assertEqual(r['DisplayName'],'project-alpha-'+r['DispatchedJobID']);self.assertEqual(api.job['Name'],r['DisplayName']);self.assertTrue(r['DispatchedJobID'].startswith('worker-'))
   self.assertEqual(w.job_lifecycle(api,r['DispatchedJobID'])['display_name'],r['DisplayName'])
   receipt=Path(td)/'.local/state/nomad-boxlite-prime/receipts'/(r['request_id']+'.json');self.assertIn(r['DisplayName'],receipt.read_text())
   self.assertEqual(sum(method=='POST' for _,method,_ in api.calls),1)
 def test_invalid_project_fails_before_api(self):
  api=API()
  with self.assertRaises(ValueError):w.main(['request','--project','https://private.example/repo'],api=api)
  self.assertEqual(api.calls,[])
 def test_denied_node_inventory_is_explicit_and_never_retries_create(self):
  api=API();policy={'provider':'fixture','default_model':'example-model','allowed_models':['example-model']}
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)),patch.object(w,'policy_catalog',return_value=catalog()),patch.object(w.auth,'protected_json',return_value=policy),patch.object(w.auth,'access',return_value={'token':'synthetic'}),patch.object(w,'node_inventory',return_value={'nodes':[],'complete':False,'phase':'node_inventory_denied','required_scope':'node:read'}),contextlib.redirect_stdout(io.StringIO()):
   r=w.main(['request','--architecture','arm64','--image','registry.example/image@sha256:'+'a'*64,'--cpus','1','--memory-mib','512'],api=api)
   self.assertEqual(r['ResourceInventory']['phase'],'node_inventory_denied');self.assertFalse(r['ResourceInventory']['accounting_complete'])
   self.assertIn('conservative maximum',r['ResourceInventory']['reservation']);self.assertEqual(sum(m=='POST' for _,m,_ in api.calls),1)
 def test_invalid_resource_request_never_reserves_or_posts(self):
  from test_universal import req
  api=API();d=req();d['cpus']=9
  with tempfile.TemporaryDirectory() as td,contextlib.redirect_stdout(io.StringIO()):
   path=Path(td)/'receipts'
   with self.assertRaisesRegex(ValueError,'every applicable'):w.dispatch(api,d,path,catalog=catalog())
   self.assertFalse(path.exists());self.assertEqual(api.calls,[])
if __name__=='__main__':unittest.main()
