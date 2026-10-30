import test_support
import unittest,json,tempfile,importlib.util,sys,io,urllib.error,http.client
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent;sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('reconnect_worker',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
JOB='worker-11111111-1111-4111-8111-111111111111';RID='22222222-2222-4222-8222-222222222222';SID='33333333-3333-4333-8333-333333333333';AID='44444444-4444-4444-8444-444444444444'
class API:
 def call(self,*a,**kw):return {'TaskStates':{'vm':{'State':'running'}},'ClientStatus':'running'}
class Response:
 def __init__(self,rows,broken=False):self.rows=rows;self.broken=broken
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def __iter__(self):
  for e in self.rows:yield (json.dumps(e)+'\n').encode()
  if self.broken:raise http.client.RemoteDisconnected()
class Reconnect(unittest.TestCase):
 def test_http500_then_recovery_only_repeats_read(self):
  api=API();calls=[]
  def once(*args):
   calls.append(args)
   if len(calls)==1:raise urllib.error.HTTPError('http://localhost',500,'temporary',{},None)
   return {'phase':'ready'}
  with patch.object(w,'_wait_session_once',once),patch.object(w.time,'sleep'),patch('sys.stdout',new_callable=io.StringIO) as out:
   self.assertEqual(w.wait_session(api,JOB)['phase'],'ready');self.assertEqual(len(calls),2);self.assertIn('observation_temporarily_unavailable',out.getvalue());self.assertIn('"prompt_replayed": false',out.getvalue())
 def test_auth_denied_is_not_retried(self):
  with patch.object(w,'_wait_session_once',side_effect=urllib.error.HTTPError('x',403,'denied',{},None)) as once,patch.object(w.time,'sleep') as sleep:
   with self.assertRaises(urllib.error.HTTPError):w.wait_session(API(),JOB)
   self.assertEqual(once.call_count,1);sleep.assert_not_called()
 def test_deadline_and_retry_limit_are_observation_states(self):
  clock=[0.0]
  with patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.time,'sleep',side_effect=lambda t:clock.__setitem__(0,clock[0]+t)),patch.object(w,'_wait_session_once',side_effect=urllib.error.HTTPError('x',500,'temporary',{},None)),patch('sys.stdout',new_callable=io.StringIO):
   d=w.wait_session(API(),JOB,deadline_seconds=1);self.assertEqual(d['phase'],'wait_deadline');self.assertTrue(d['session_not_stopped'])
 def test_disconnect_resumes_consumed_cursor_not_snapshot_head(self):
  responses=[Response([{'event':'snapshot','sequence':100,'epoch':'same'},{'event':'progress','sequence':4,'epoch':'same'}],True),Response([{'event':'snapshot','sequence':101,'epoch':'same'},{'event':'progress','sequence':4,'epoch':'same'},{'event':'request_terminal','sequence':5,'epoch':'same','request_id':RID}])]
  opener=unittest.mock.Mock();opener.open.side_effect=responses;access={'allocation_id':AID,'session_id':SID,'log_task':'vm','host':'192.0.2.20','port':20000,'token':'fixture'}
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)),patch.object(w,'job_lifecycle',return_value={'stop':False}),patch.object(w,'allocations',return_value=[{'ID':AID}]),patch.object(w,'session_access',return_value=access),patch.object(w,'checked_status',return_value={'guest_ready':True,'guest':{'requests':[]}}),patch.object(w,'stream_lines',return_value=iter([json.dumps({'event':'gateway_listening'}).encode()])),patch.object(w.urllib.request,'build_opener',return_value=opener),patch.object(w,'endpoint',return_value={'request_id':RID}),patch.object(w.time,'sleep'),patch('sys.stdout',new_callable=io.StringIO):
   # Each reconnect gets its own retained bootstrap stream.
   with patch.object(w,'stream_lines',side_effect=lambda *a:iter([json.dumps({'event':'gateway_listening'}).encode()])):
    d=w.wait_session(API(),JOB,'result',RID);self.assertEqual(d['request_id'],RID)
   self.assertIn('after=4',opener.open.call_args_list[1].args[0].full_url)
 def test_replay_gap_is_explicit_not_retried(self):
  with patch.object(w,'_wait_session_once',return_value={'phase':'replay_gap','cursor':4}) as once:
   self.assertEqual(w.wait_session(API(),JOB)['phase'],'replay_gap');self.assertEqual(once.call_count,1)

class Capacity(unittest.TestCase):
 def test_visible_reservations_are_not_usage_or_global_free(self):
  from test_universal import catalog,NODE
  class CapacityAPI:
   def call(self,path,*args):
    if path=='/v1/allocations':return [{'ID':AID,'ClientStatus':'running'}]
    return {'ID':AID,'JobID':JOB,'NodeID':NODE,'TaskGroup':'worker','ClientStatus':'running','DesiredStatus':'run','Job':{'Name':'project-alpha-'+JOB,'TaskGroups':[{'Name':'worker','Tasks':[{'Name':'vm','Driver':'boxlite','Config':{'guest_cpus':2,'memory_mib':4096},'Resources':{'CPU':6000,'MemoryMB':5120}}]}]}}
  with patch.object(w,'policy_catalog',return_value=catalog()),patch.object(w,'has_local_receipt',return_value=True),patch.object(w,'session_access',return_value={}),patch.object(w,'active_session',return_value={'state':'ready','requests':[]}):
   d=w.capacity_summary(CapacityAPI());n=d['nodes'][NODE];self.assertEqual(n['current_namespace_reservations']['guest_ram_mib'],4096);self.assertEqual(n['current_namespace_reservations']['overhead_mib'],1024);self.assertEqual(n['remaining_from_visible_reservations']['vms'],3);self.assertEqual(n['remaining_from_visible_reservations']['guest_vcpus'],6);self.assertTrue(d['sessions'][0]['idle_candidate']);self.assertFalse(d['completeness']['global_free_capacity_confirmed']);self.assertFalse(n['scheduler_capacity']['available']);self.assertIsNone(d['sessions'][0]['usage']['guest_mem_used_bytes'])
 def test_missing_allocation_is_incomplete_not_false_free(self):
  from test_universal import catalog
  class BrokenAPI:
   def call(self,path,*args):
    if path=='/v1/allocations':return [{'ID':AID,'ClientStatus':'running'}]
    raise urllib.error.HTTPError('x',500,'temporary',{},None)
  with patch.object(w,'policy_catalog',return_value=catalog()):
   d=w.capacity_summary(BrokenAPI());self.assertFalse(d['completeness']['namespace_allocations_complete']);self.assertEqual(d['errors'][0]['stage'],'allocation_reservations')

class SemanticMessages(unittest.TestCase):
 def test_explicit_messages_use_authenticated_channel_and_redact_secrets(self):
  access={'allocation_id':AID,'session_id':SID,'host':'192.0.2.20','port':20000,'token':'a'*64}
  rows=[{'event':'assistant_delta','sequence':1,'text':'Preparing tests LLM_API_KEY=fixture-private'}, {'event':'agent_progress','sequence':2,'message_kind':'plan','text':'Run browser tests','plan_entry_count':1},{'event':'agent_thought_chunk','sequence':3,'text':'PRIVATE_THOUGHT'}]
  opener=unittest.mock.Mock();opener.open.side_effect=lambda *a,**kw:Response(rows)
  with patch.object(w,'job_lifecycle',return_value={'stop':False}),patch.object(w,'session_access',return_value=access),patch.object(w,'active_session',return_value={'state':'busy','requests':[],'active_request_id':RID,'running_elapsed_ms':1234}),patch.object(w.urllib.request,'build_opener',return_value=opener):
   plain=w.activity(API(),JOB);self.assertEqual(plain['recent_messages'],[])
   text=w.activity(API(),JOB,include_messages=True);self.assertEqual(text['running_elapsed_ms'],1234);self.assertEqual(len(text['recent_messages']),2);raw=json.dumps(text);self.assertNotIn('PRIVATE_THOUGHT',raw);self.assertNotIn('fixture-private',raw);self.assertIn('Preparing tests',raw);self.assertNotIn('text',text['recent_metadata'][0])

class ReconnectProgressBudget(unittest.TestCase):
 def test_delivered_events_reset_consecutive_failure_budget(self):
  attempts=[0]
  def once(*args):
   attempts[0]+=1
   if attempts[0]<=10:
    args[-1]['after']+=1
    raise urllib.error.HTTPError('x',500,'temporary',{},None)
   return {'phase':'ready'}
  with patch.object(w,'_wait_session_once',side_effect=once),patch.object(w.time,'sleep'),patch('sys.stdout',new_callable=io.StringIO):
   self.assertEqual(w.wait_session(API(),JOB)['phase'],'ready');self.assertEqual(attempts[0],11)
 def test_eight_failures_without_events_return_observation_unavailable(self):
  with patch.object(w,'_wait_session_once',side_effect=urllib.error.HTTPError('x',500,'temporary',{},None)) as once,patch.object(w.time,'sleep'),patch('sys.stdout',new_callable=io.StringIO):
   d=w.wait_session(API(),JOB);self.assertEqual(once.call_count,8);self.assertEqual(d['phase'],'observation_unavailable');self.assertTrue(d['guest_state_unknown'])
