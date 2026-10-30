import unittest,json,importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('noise_observability',Path(__file__).with_name('observability.py'));o=importlib.util.module_from_spec(spec);spec.loader.exec_module(o)
class Tests(unittest.TestCase):
 def test_default_drops_chunks_and_duplicate_tool_trace(self):
  for tag in o.CHUNK_UPDATES|o.DUPLICATE_TOOL_UPDATES:
   self.assertIsNone(o.project({'event':'acp_trace','action':'notification','update_type':tag,'text':'PRIVATE'}))
 def test_opt_in_is_metadata_only(self):
  for tag in o.CHUNK_UPDATES|o.DUPLICATE_TOOL_UPDATES:
   p=o.project({'event':'acp_trace','action':'notification','update_type':tag,'text':'PRIVATE','params':{'token':'SECRET'}},chunk_metadata=True)
   self.assertEqual(p['update_type'],tag);self.assertNotIn('PRIVATE',json.dumps(p));self.assertNotIn('SECRET',json.dumps(p))
 def test_control_errors_progress_and_lifecycle_retained(self):
  for e in [{'event':'acp_trace','action':x} for x in ['send','receive','error','timeout','disconnect']]+[{'event':x,'state':'running'} for x in ['request_accepted','request_running','request_terminal']]+[{'event':'progress','kind':'execute','status':'completed','elapsed_ms':13},{'event':'agent_progress','message_kind':'plan','plan_entry_count':1}]:
   self.assertIsNotNone(o.project(e))
 def test_protected_operator_policy_only(self):
  self.assertEqual(o.logging_policy(None,lambda p:(_ for _ in ()).throw(FileNotFoundError())),{'chunk_metadata':False})
  self.assertTrue(o.logging_policy(None,lambda p:{'chunk_metadata':True})['chunk_metadata'])
  for x in [{'chunk_metadata':1},{'chunk_metadata':'true'},{'extra':True},{}]:
   with self.assertRaises(ValueError):o.logging_policy(None,lambda p:x)
 def test_after_ready_plain_stderr_is_runtime_not_secret_provider(self):
  before=o.plain_output_metadata('stderr','Traceback PRIVATE token=SECRET',False);after=o.plain_output_metadata('stderr','Traceback PRIVATE token=SECRET',True)
  self.assertEqual(before['event'],'secret_bootstrap_log');self.assertEqual(after['event'],'runtime_stderr');self.assertEqual(after['layer'],'companion_runtime');self.assertNotIn('PRIVATE',json.dumps(after));self.assertNotIn('SECRET',json.dumps(after))
 def test_existing_bridge_projects_before_stdout_and_preserves_replay(self):
  s=Path(__file__).with_name('acp_bridge.py').read_text();compile(s,'bridge','exec')
  self.assertIn("chunk_metadata=log_policy['chunk_metadata']",s)
  self.assertLess(s.index('metadata=observability.project'),s.index('if metadata:'))
  self.assertIn("journal.append(json.loads(safe(public)))",s)
  self.assertIn('secure_logs.capture_and_mirror(spool,target,kind,raw,delivery_unavailable)',s)
if __name__=='__main__':unittest.main()
