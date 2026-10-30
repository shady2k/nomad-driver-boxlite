import test_support
import unittest,sys,json,tempfile,io
from pathlib import Path
HERE=Path(__file__).parent;sys.path.insert(0,str(HERE))
import observability as o
class RuntimeMetadata(unittest.TestCase):
 def test_correlated_tool_metadata_without_any_payload(self):
  context={'allocation_id':'00000000-0000-4000-8000-000000000012','session_id':'00000000-0000-4000-8000-000000000013','vm_id':'fixture-vm'}
  event={'event':'progress','request_id':'00000000-0000-4000-8000-000000000014','kind':'execute','status':'failed','elapsed_ms':12,'tool_call_id_sha256':'a'*16,'text':'PRIVATE_PROMPT','message':'FILE_CONTENT','rawInput':{'command':'secret args'},'error':{'code':'provider_timeout','message':'Bearer LEAK https://host?token=SECRET'}}
  p=o.project(event,context,now=1);raw=json.dumps(p)
  for value in ['PRIVATE_PROMPT','FILE_CONTENT','secret args','LEAK','SECRET','Bearer']:self.assertNotIn(value,raw)
  self.assertEqual(p['request_id'],event['request_id']);self.assertEqual(p['session_id'],context['session_id']);self.assertEqual(p['tool_name'],'execute');self.assertEqual(p['elapsed_ms'],12);self.assertEqual(p['error_code'],'provider_timeout')
 def test_acp_observation_is_not_raw_rpc_or_inferred_wait(self):
  p=o.project({'event':'acp_trace','action':'notification','operation':'turn.events','observation':'normalized_event','method':'session/update','update_type':'tool_call','rpc_id':'SECRET','text':'MODEL_REPLY','params':{'prompt':'PRIVATE'}},now=1,chunk_metadata=True)
  self.assertFalse(p['rpc_id_available']);self.assertNotIn('rpc_id',p);self.assertNotIn('params',p);self.assertNotIn('text',p);self.assertEqual(p['method'],'session/update')
  self.assertIsNone(o.project({'event':'assistant_delta','text':'DO_NOT_LOG'}));self.assertIsNone(o.project({'event':'acp_trace','action':'waiting_for_model'}))
 def test_malformed_guest_metadata_cannot_crash_or_leak(self):
  for key in ['event','action','operation','method','state','status','kind','error_code','error_type','signal']:
   e={'event':'acp_trace','action':'error',key:{'secret':'DO_NOT_LOG'},'error':{'code':['LEAK']},'timestamp':10**1000,'elapsed_ms':float('nan')}
   p=o.project(e)
   self.assertNotIn('DO_NOT_LOG',json.dumps(p));self.assertNotIn('LEAK',json.dumps(p))
if __name__=='__main__':unittest.main()

class Activity(unittest.TestCase):
 def test_existing_event_channel_readonly_metadata_view(self):
  import importlib.util
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('activity_worker',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
  aid='00000000-0000-4000-8000-000000000012';sid='00000000-0000-4000-8000-000000000013';rid='00000000-0000-4000-8000-000000000014'
  frames=[{'event':'snapshot','sequence':3,'snapshot':{'state':'busy'}},{'event':'assistant_delta','sequence':1,'time':1,'text':'PRIVATE_MODEL_REPLY'}, {'event':'progress','sequence':2,'time':2,'request_id':rid,'kind':'execute','status':'completed','rawInput':{'command':'SECRET_COMMAND'}},{'event':'request_terminal','sequence':3,'time':3,'request_id':rid,'state':'completed','snapshot':{'state':'ready'},'error':None},{'event':'wait_deadline','sequence':3}]
  class Opener:
   def open(self,request,timeout):
    self.request=request
    return io.BytesIO(('\n'.join(json.dumps(e) for e in frames)+'\n').encode())
  access={'allocation_id':aid,'session_id':sid,'host':'192.0.2.20','port':20000,'token':'a'*64};sink=io.StringIO()
  with patch.object(w,'job_lifecycle',return_value={'stop':False}),patch.object(w,'session_access',return_value=access),patch.object(w,'active_session',return_value={'state':'busy','requests':[{'request_id':rid,'state':'running'}]}),patch.object(w.urllib.request,'build_opener',return_value=Opener()),patch('sys.stdout',sink):
   result=w.activity(None,'worker-fixture',1,0,True)
  text=json.dumps(result)+sink.getvalue()
  for private in ['PRIVATE_MODEL_REPLY','SECRET_COMMAND','a'*64]:self.assertNotIn(private,text)
  self.assertEqual(result['state'],'ready');self.assertEqual(result['last_observed_tool']['tool_name'],'execute');self.assertEqual(result['last_observed_tool']['status'],'completed');self.assertEqual(result['last_observed_event']['event'],'request_terminal')

class ProcessTrace(unittest.TestCase):
 def test_disconnect_has_no_guessed_request_or_process_arguments(self):
  e={'event':'acp_trace','action':'disconnect','operation':'agent_process','observation':'process_lifecycle','exit_code':2,'pid':123,'signal':'SIGTERM','args':['SECRET'],'cwd':'PRIVATE_PATH'}
  p=o.project(e,{'request_id':'00000000-0000-4000-8000-000000000014','session_id':'00000000-0000-4000-8000-000000000013'})
  self.assertNotIn('request_id',p);self.assertEqual(p['exit_code'],2);self.assertEqual(p['signal'],'SIGTERM');self.assertNotIn('SECRET',json.dumps(p));self.assertNotIn('PRIVATE_PATH',json.dumps(p))

class SemanticProgressMetadata(unittest.TestCase):
 def test_plan_text_stays_out_of_infrastructure_logs(self):
  import observability
  d=observability.project({'event':'agent_progress','message_kind':'plan','text':'PRIVATE_PLAN','entries':[{'content':'PRIVATE_FILE'}],'plan_entry_count':1,'elapsed_ms':500})
  self.assertEqual(d['message_kind'],'plan');self.assertEqual(d['plan_entry_count'],1);self.assertNotIn('PRIVATE',json.dumps(d));self.assertNotIn('text',d);self.assertNotIn('entries',d)
