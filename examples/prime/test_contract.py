import test_support
import importlib.util,json,sys,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import profiles,gateway,contract
if __name__=='__main__':unittest.main()

class PristineChildContext(unittest.TestCase):
 def test_only_nonsecret_context_survives_pristine_child(self):
  import acp_bridge,tempfile,os,uuid
  from unittest.mock import patch
  root=Path(tempfile.mkdtemp());aid=str(uuid.uuid4());d=root/'sessions'/aid;d.mkdir(parents=True)
  ctx={k:'fixture' for k in acp_bridge.CONTEXT_KEYS};ctx['NOMAD_ALLOC_ID']=aid
  f=d/'context.json';f.write_text(json.dumps(ctx));f.chmod(0o600)
  with patch.object(acp_bridge,'ROOT',root),patch.dict(os.environ,{},clear=True):
   acp_bridge.restore_context(aid);self.assertEqual(set(os.environ),acp_bridge.CONTEXT_KEYS)
   self.assertNotIn('BOXLITE_REST_API_KEY',os.environ);self.assertNotIn('LLM_API_KEY',os.environ)


class CoordinatorPermissions(unittest.TestCase):
 def test_status_binding_requires_no_node_acl(self):
  import importlib.util,uuid
  from unittest.mock import patch
  file=HERE/'claude-plugin/scripts/workerctl.py';spec=importlib.util.spec_from_file_location('test_workerctl',file);w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
  job='legacy-worker/dispatch-123-fixture';aid=str(uuid.uuid4());sid=str(uuid.uuid4())
  class API:
   def call(self,path):
    if path.startswith('/v1/node/'):raise AssertionError('must not widen node ACL')
    if path.endswith('/allocations'):return [{'ID':aid,'NodeID':'native-compute'}]
    if path.startswith('/v1/allocation/'):return {'AllocatedResources':{'Shared':{'Ports':[{'Label':'session','HostIP':'192.0.2.20','Value':20000}]}}}
    return {'Namespace':'boxlite-prime','ParentID':'legacy-worker','Meta':{'session_id':sid},'TaskGroups':[{'Tasks':[{'Name':'vm','Driver':'boxlite'}]}]}
  with patch.object(w.auth,'access',lambda s,p:{'session_id':s,'token':'fixture'}):
   r=w.session_access(API(),job);self.assertEqual(r['allocation_id'],aid);self.assertEqual(r['host'],'192.0.2.20')

class GatewayReadiness(unittest.TestCase):
 def test_connection_refused_is_not_guest_ready_or_dispatch_retry(self):
  import importlib.util,uuid,urllib.error
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('readiness_workerctl',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
  rows=[{'ID':str(uuid.uuid4()),'ClientStatus':'running','DesiredStatus':'run','TaskStates':{}}]
  with patch.object(w,'job_lifecycle',lambda *a:{'stop':False}),patch.object(w,'allocations',lambda api,job:rows),patch.object(w,'session_access',lambda api,job:{}),patch.object(w,'active_session',side_effect=urllib.error.URLError(ConnectionRefusedError())):
   r=w.checked_status(None,'legacy-worker/dispatch-123-fixture');self.assertFalse(r['guest_ready']);self.assertEqual(r['phase'],'starting');self.assertEqual(r['guest']['state'],'gateway_unreachable')

class UniversalResourceInterface(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('universal_workerctl',HERE/'claude-plugin/scripts/workerctl.py');self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
  from unittest.mock import patch
  guard=patch.object(self.w.auth,'protected_json',return_value={'provider':'operator-provider','default_model':'model-default','allowed_models':['model-default']});guard.start();self.addCleanup(guard.stop)
 def policy(self):
  return {'variable':'boxlite-prime:nomad/jobs','modify_index':1,'nodes':{'00000000-0000-4000-8000-000000000001':{'max_vms':4,'max_guest_cpus':8,'max_reservation_mib':24576,'memory_overhead_mib':1024,'cpu_mhz_per_vcpu':3000,'default_disk_gib':12,'max_disk_gib':12}}}
 def test_request_has_explicit_resources_and_policy_disk_default(self):
  from unittest.mock import patch
  captured={}
  with self.assertRaises(ValueError):self.w.main(['request'],api=object())
  def dispatch(api,d,path,project=None,**kwargs):captured.update(d);return {'DispatchedJobID':'fixture'}
  with patch.object(self.w.auth,'access',lambda *a:{}),patch.object(self.w,'policy_catalog',lambda api:self.policy()),patch.object(self.w,'node_inventory',lambda api:{'nodes':[],'complete':True}),patch.object(self.w,'dispatch',dispatch):
   self.w.main(['request','--architecture','amd64','--image','registry.example/image@sha256:'+'a'*64,'--cpus','3','--memory-mib','3072'],api=object())
  self.assertEqual(captured['cpus'],3);self.assertEqual(captured['memory_mib'],3072);self.assertEqual(captured['disk_gib'],12);self.assertNotIn('profile',captured);self.assertNotIn('node_id',captured);self.assertEqual(captured['architecture'],'amd64')
 def test_discovery_is_live_policy_not_local_constants(self):
  from unittest.mock import patch
  with patch.object(self.w,'policy_catalog',lambda api:self.policy()):self.assertEqual(self.w.main(['resources'],api=object())['modify_index'],1)
 def test_missing_policy_cannot_dispatch(self):
  from unittest.mock import patch
  with patch.object(self.w,'policy_catalog',side_effect=ValueError('missing policy')),patch.object(self.w,'dispatch',side_effect=AssertionError('must not create')):
   with self.assertRaisesRegex(ValueError,'missing policy'):self.w.main(['request','--image','x@sha256:'+'a'*64,'--cpus','1','--memory-mib','512'],api=object())

class NomadOriginPolicy(unittest.TestCase):
 def test_operator_origin_requires_https_or_loopback(self):
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('origin_workerctl',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
  for value in ['https://scheduler.example','http://127.0.0.1:4646']:
   with patch.object(w.Path,'exists',return_value=True),patch.object(w.auth,'protected_json',return_value={'nomad_address':value}):self.assertEqual(w.configured_nomad_origin(),value)
  for value in ['http://scheduler.example','https://user:secret@scheduler.example','https://scheduler.example/path','https://scheduler.example?token=value','file:///tmp/x',None]:
   with patch.object(w.Path,'exists',return_value=True),patch.object(w.auth,'protected_json',return_value={'nomad_address':value}):
    with self.assertRaises(ValueError):w.configured_nomad_origin()
