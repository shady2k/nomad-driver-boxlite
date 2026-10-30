import test_support
import unittest, tempfile, json, subprocess, os, sys
from pathlib import Path
from unittest.mock import patch
import observability as o
HERE=Path(__file__).parent
class TaskOutcome(unittest.TestCase):
 def test_projection_never_trusts_task_success_or_payload(self):
  value={'state':'completed','stop_reason':'max_tokens','task_result':{'state':'success','completion_verified':True,'payload':'SECRET'}}
  result=o.result_view(value)
  self.assertEqual(result['state'],'completed');self.assertEqual(result['acp_status'],'completed');self.assertEqual(result['phase'],'limited');self.assertEqual(result['task_result']['state'],'incomplete');self.assertFalse(result['task_result']['completion_verified']);self.assertFalse(result['task_result']['automatic_retry']);self.assertNotIn('SECRET',json.dumps(result))
  terminal=o.project({'event':'request_terminal','state':'completed','terminal_metadata':{'stop_reason':'max_tokens'},'prompt':'SECRET'},now=1)
  self.assertEqual(terminal['task_result']['state'],'incomplete');self.assertNotIn('SECRET',json.dumps(terminal))
 def test_failed_and_unknown_never_become_success_or_limit(self):
  for state,expected in [('failed_no_retry','failed'),('uncertain_no_retry','unknown')]:
   self.assertEqual(o.task_result({'state':state,'stop_reason':'max_tokens'})['state'],expected)
  for v in [None,{},[],True]:
   self.assertFalse(o.task_result({'state':'completed','stop_reason':v})['completion_verified'])
 def test_cached_cli_survives_vm_stop_and_signals_limit_without_remote_access(self):
  rid='11111111-1111-4111-8111-111111111111';job='worker-22222222-2222-4222-8222-222222222222'
  for reason,code,state in [('max_tokens',7,'incomplete'),('end_turn',0,'unverified')]:
   with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/'.local/state/nomad-boxlite-prime/results';root.mkdir(parents=True,mode=0o700);path=root/(rid+'.json');path.write_text(json.dumps({'job':job,'result':{'request_id':rid,'state':'completed','stop_reason':reason,'text':'partial','tool_summary':{'completed':1}}}));path.chmod(0o600)
    p=subprocess.run([sys.executable,str(HERE/'claude-plugin/scripts/workerctl.py'),'result',job,rid,'--saved'],env={**os.environ,'HOME':temp,'NOMAD_ADDR':'http://127.0.0.1:1'},capture_output=True,text=True,timeout=5)
    self.assertEqual(p.returncode,code,p.stderr);out=json.loads(p.stdout);self.assertEqual(out['state'],'completed');self.assertEqual(out['acp_status'],'completed');self.assertEqual(out['task_result']['state'],state);self.assertEqual(out['text'],'partial');self.assertEqual(out['tool_summary']['completed'],1);self.assertEqual(out['result_source'],'saved_no_remote_access')
