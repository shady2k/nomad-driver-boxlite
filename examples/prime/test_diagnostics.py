import test_support
import unittest,importlib.util,sys,tempfile,json,io,urllib.error
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('diagnostic_worker',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
spec=importlib.util.spec_from_file_location('diagnostic_guest',HERE/'guest/guest.py');g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
class Response(io.BytesIO):
 headers={'Docker-Content-Digest':'sha256:'+'a'*64}
class Diagnostics(unittest.TestCase):
 def test_worker_compatibility_is_explicit(self):
  f=w.worker_compatibility;arm=[{'os':'linux','architecture':'arm64'}]
  self.assertEqual(f(arm,{},'arm64')['state'],'unknown')
  self.assertEqual(f(arm,{'io.boxlite.worker-contract':'2'},'amd64')['state'],'incompatible')
  self.assertEqual(f(arm,{'io.boxlite.worker-contract':'1'},'arm64')['state'],'incompatible')
  good=f(arm,{'io.boxlite.worker-contract':'2','io.boxlite.prime-version':'0.10.1'},'aarch64')
  self.assertEqual(good['state'],'declared_compatible');self.assertFalse(good['runtime_verified'])
  self.assertEqual(f(arm,{'io.boxlite.worker-contract':'2'},None)['state'],'unknown')
 def test_workers_filter_keeps_unknown_visible(self):
  def read(req,timeout):
   url=req.full_url
   value={'repositories':['generic/image']} if '_catalog' in url else {'tags':['v1']} if 'tags/list' in url else {'config':{'digest':'sha256:123'}} if '/manifests/' in url else {'os':'linux','architecture':'arm64'}
   return Response(json.dumps(value).encode())
  with patch.object(w.urllib.request,'urlopen',read):
   d=w.registry_images('https://registry.example',workers_only=True,architecture='arm64')
   self.assertEqual(d['images'],[]);self.assertEqual(len(d['unknown_images']),1);self.assertFalse(d['empty'])
 def test_discovery_actual_metadata(self):
  def read(req,timeout):
   url=req.full_url
   value={'repositories':['some/runtime']} if '_catalog' in url else {'tags':['v1']} if 'tags/list' in url else {'config':{'digest':'sha256:123'}} if '/manifests/' in url else {'os':'linux','architecture':'arm64'}
   return Response(json.dumps(value).encode())
  with patch.object(w.urllib.request,'urlopen',read):
   result=w.registry_images('https://registry.example');self.assertEqual(result['images'][0]['platforms'][0]['architecture'],'arm64');self.assertIn('@sha256:',result['images'][0]['image'])
 def test_discovery_denied_not_empty_or_missing(self):
  with patch.object(w.urllib.request,'urlopen',side_effect=urllib.error.HTTPError('x',403,'denied',{},None)):
   with self.assertRaisesRegex(ValueError,'registry_denied'):w.registry_images('https://registry.example')
 def test_log_gc_explicit(self):
  class API:
   def call(self,*a,**kw):raise urllib.error.HTTPError('x',404,'gone',{},None)
  with patch.object(w,'session_access',return_value={'allocation_id':'test','log_task':'vm'}):self.assertEqual(w.read_logs(API(),'worker-11111111-1111-4111-8111-111111111111')['cause'],'allocation_logs_expired_or_client_state_missing')
 def test_timeout_emits_stage_without_command_output(self):
  with tempfile.TemporaryDirectory() as d,patch.object(g,'WORK',Path(d)),patch('sys.stdout',new_callable=io.StringIO) as out:
   g.STAGE='repo_clone'
   with self.assertRaises(TimeoutError):g.bounded_command(['/bin/sleep','1'],.01)
   records=[json.loads(x) for x in out.getvalue().splitlines()];self.assertEqual(records[-1]['exception_type'],'TimeoutError');self.assertEqual(records[-1]['stage'],'repo_clone');self.assertNotIn('/bin/sleep',out.getvalue())

 def test_summary_survives_client_then_server_gc(self):
  job='worker-11111111-1111-4111-8111-111111111111'
  class API:
   gone=False
   def call(self,*a,**kw):
    if self.gone:raise urllib.error.HTTPError('x',404,'gone',{},None)
    return [{'ID':'allocation-test','TaskStates':{'vm':{'Events':[{'Type':'Driver','Time':1,'DriverMessage':'boxlite terminal_failure_summary; summary='+json.dumps({'stage':'repo_clone','exception_type':'TimeoutError','reason':'deadline_exceeded','deadline_seconds':'120'})}]}}}]
  api=API()
  with tempfile.TemporaryDirectory() as d,patch.object(w.Path,'home',return_value=Path(d)):
   first=w.terminal_diagnostics(api,job);self.assertEqual(first['source'],'nomad_server_task_events')
   api.gone=True;second=w.terminal_diagnostics(api,job);self.assertEqual(second['source'],'coordinator_saved_summary');self.assertEqual(second['summaries'],first['summaries']);self.assertFalse(second['raw_logs_restored'])

class FullLogs(unittest.TestCase):
 def test_cached_full_log_remains_readable_after_gc(self):
  job='worker-11111111-1111-4111-8111-111111111111'
  class API:
   gone=False
   def call(self,*args,**kw):
    if self.gone:raise urllib.error.HTTPError('x',404,'gone',{},None)
    return 'Traceback (most recent call last):\nRuntimeError: native module missing\n'
  api=API()
  with tempfile.TemporaryDirectory() as d,patch.object(w.Path,'home',return_value=Path(d)),patch.object(w,'session_access',return_value={'allocation_id':'test','log_task':'vm'}):
   first=w.read_logs(api,job,'stderr');self.assertTrue(first['complete_snapshot']);self.assertIn('Traceback',first['logs'])
   second=w.read_logs(api,job,'stderr');self.assertNotEqual(first['local_path'],second['local_path']);self.assertTrue(Path(first['local_path']).exists());self.assertTrue(Path(second['local_path']).exists())
   api.gone=True;later=w.read_logs(api,job,'stderr');self.assertEqual(later['http_status'],404);self.assertEqual(later['saved_snapshot']['sha256'],first['sha256'])
   self.assertEqual(w.saved_logs(job,'stderr')['logs'],first['logs'])

class JobHistory(unittest.TestCase):
 def api(self,stop=True,client='failed'):
  class API:
   def call(self,path,*a,**kw):
    if path.endswith('/allocations'):return [{'ID':'00000000-0000-4000-8000-000000000010','ClientStatus':client,'DesiredStatus':'run','JobVersion':0,'TaskStates':{}}]
    return {'Stop':stop,'Status':'dead' if stop else 'running','Version':1}
  return API()
 def test_stopped_job_failed_desired_run_is_history_not_live_vm(self):
  job='worker-00000000-0000-4000-8000-000000000011'
  with patch.object(w,'session_access',side_effect=AssertionError('No seed/gateway query for stopped job')):
   d=w.checked_status(self.api(),job)
  self.assertEqual(d['phase'],'job_stopped');self.assertFalse(d['guest_ready']);self.assertEqual(d['guest']['native_vm_state'],'unverified')
  self.assertTrue(d['nomad']['job_lifecycle']['stop']);self.assertTrue(d['nomad']['allocations'][0]['older_job_version']);self.assertTrue(d['nomad']['allocations'][0]['terminal_history'])
 def test_stopped_job_with_running_allocation_does_not_claim_native_vm_stopped(self):
  d=w.checked_status(self.api(client='running'),'worker-00000000-0000-4000-8000-000000000011')
  self.assertEqual(d['phase'],'job_stopping');self.assertEqual(d['guest']['native_vm_state'],'unverified')
 def test_failed_allocation_in_unstopped_job_is_not_labeled_job_stopped(self):
  d=w.checked_status(self.api(stop=False),'worker-00000000-0000-4000-8000-000000000011')
  self.assertEqual(d['phase'],'failed');self.assertFalse(d['nomad']['job_lifecycle']['stop'])

 def test_wait_stopped_job_returns_without_seed_or_log_subscription(self):
  with patch.object(w,'session_access',side_effect=AssertionError('No seed needed')),patch.object(w,'stream_lines',side_effect=AssertionError('No stopped-job subscription')):
   d=w.wait_session(self.api(),'worker-00000000-0000-4000-8000-000000000011')
  self.assertEqual(d['phase'],'job_stopped')
