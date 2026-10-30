import test_support
import unittest,tempfile,threading,time,os,json,sys,importlib.util,hashlib,base64,subprocess
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent;sys.path.insert(0,str(HERE))
import events,acp_bridge
spec=importlib.util.spec_from_file_location('artifact_operations',HERE/'guest/artifacts.py');art=importlib.util.module_from_spec(spec);spec.loader.exec_module(art)
class Contract(unittest.TestCase):
 def test_missing_secret_is_explicit_and_does_not_consume_other_field(self):
  with patch.dict(os.environ,{'LLM_API_KEY':'fixture-value'},clear=True):
   with self.assertRaisesRegex(RuntimeError,'required_secret_fields_missing: BOXLITE_DISPATCHER_SEED'):
    acp_bridge.required_secret_material()
   self.assertEqual(os.environ['LLM_API_KEY'],'fixture-value')
  with patch.dict(os.environ,{'LLM_API_KEY':'fixture-value','BOXLITE_DISPATCHER_SEED':'a'*64},clear=True):
   key,seed=acp_bridge.required_secret_material()
   self.assertEqual(key,'fixture-value');self.assertEqual(len(seed),32)
   self.assertNotIn('BOXLITE_DISPATCHER_SEED',os.environ)
 def test_empty_invalid_and_fresh_child_environments(self):
  for seed in ['', 'invalid']:
   with patch.dict(os.environ,{'LLM_API_KEY':'fixture-value','BOXLITE_DISPATCHER_SEED':seed},clear=True):
    with self.assertRaises(RuntimeError):acp_bridge.required_secret_material()
    self.assertEqual(os.environ['LLM_API_KEY'],'fixture-value');self.assertEqual(os.environ['BOXLITE_DISPATCHER_SEED'],seed)
  for _ in range(2):
   with patch.dict(os.environ,{'LLM_API_KEY':'fixture-value','BOXLITE_DISPATCHER_SEED':'b'*64},clear=True):
    self.assertEqual(len(acp_bridge.required_secret_material()[1]),32)
 def test_event_wait_replay_restart_and_gap(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'events.jsonl';j=events.Journal(p,max_events=2);j.append({'event':'first'});j.append({'event':'second'})
   stream=j.stream(1,time.monotonic()+1);self.assertEqual(next(stream)['event'],'snapshot');self.assertEqual(next(stream)['event'],'second');stream.close()
   j.append({'event':'third'});j2=events.Journal(p,max_events=2);self.assertEqual(j2.seq,3)
   stream=j2.stream(0,time.monotonic()+.01);next(stream);self.assertEqual(next(stream)['event'],'replay_gap');stream.close()
 def test_subscription_wakes_on_event_not_status_calls(self):
  with tempfile.TemporaryDirectory() as d:
   j=events.Journal(Path(d)/'e');seen=[]
   def consume():
    for e in j.stream(0,time.monotonic()+1):
     if e['event']=='ready':seen.append(e);break
   t=threading.Thread(target=consume);t.start();j.append({'event':'ready'});t.join(2);self.assertEqual(len(seen),1)
 def test_large_file_many_files_chunks_integrity_and_safety(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);raw=os.urandom(180000);(root/'big.bundle').write_bytes(raw)
   for i in range(20):(root/str(i)).write_text('test')
   m=art.operate({'operation':'manifest','paths':['big.bundle']+[str(i) for i in range(20)]},d);self.assertEqual(len(m['files']),21)
   first=m['files'][0];out=b''
   while len(out)<len(raw):
    q=art.operate({'operation':'chunk','path':'big.bundle','identity':first['identity'],'offset':len(out)},d);b=base64.b64decode(q['base64']);self.assertEqual(hashlib.sha256(b).hexdigest(),q['sha256']);out+=b
   self.assertEqual(raw,out);(root/'link').symlink_to('/etc/hosts')
   for name in ['../escape','link','.git/config']:
    with self.assertRaises((ValueError,OSError)):art.operate({'operation':'manifest','paths':[name]},d)
   (root/'big.bundle').write_bytes(b'changed')
   with self.assertRaises(ValueError):art.operate({'operation':'chunk','path':'big.bundle','identity':first['identity'],'offset':0},d)
 def test_brief_resumable_idempotent_hash_checked(self):
  with tempfile.TemporaryDirectory() as d:
   data=b'brief '*14000;digest=hashlib.sha256(data).hexdigest()
   for offset in range(0,len(data),40000):
    q={'operation':'brief','sha256':digest,'offset':offset,'base64':base64.b64encode(data[offset:offset+40000]).decode(),'final':offset+40000>=len(data)}
    art.operate(q,d);art.operate(q,d)
   self.assertEqual((Path(d)/'.nomad-briefs'/digest).read_bytes(),data)
 def bootstrap(self,mode):
  root=Path(tempfile.mkdtemp());aid='11111111-1111-4111-8111-111111111111';fake=root/'fake'
  fake.write_text("#!/bin/sh\n"+("echo '"+json.dumps({'event':'secret_bootstrap_ready'})+"'\n" if mode=='ready' else "")+("echo 'Traceback: non-JSON bootstrap fixture' >&2\necho 'LLM_API_KEY=fixture-NEVER-LOG' >&2\nexit 2\n" if mode=='error' else ("sleep .1\n" if mode=='ready' else "sleep 10\n")));fake.chmod(0o700)
  cfg={'vault_token_file':'unused','envconsul_binary':str(fake),'python_binary':sys.executable,'vault_addr':'https://example.invalid'}
  env={k:'fixture' for k in acp_bridge.CONTEXT_KEYS};env['NOMAD_ALLOC_ID']=aid
  with patch.object(acp_bridge,'ROOT',root),patch.object(acp_bridge,'config',lambda:cfg),patch.object(acp_bridge,'identity',lambda:(aid,{})),patch.object(acp_bridge,'BOOTSTRAP_SECONDS',3),patch.object(sys,'argv',['bridge']),patch.dict(os.environ,env,clear=True):
   if mode=='ready':self.assertEqual(acp_bridge.main(),0)
   else:
    with self.assertRaises(TimeoutError if mode=='hang' else RuntimeError):acp_bridge.main()
  result=json.loads((root/'sessions'/aid/'bootstrap.jsonl').read_text().splitlines()[0])
  result['spool_text']=''.join(p.read_text() for p in (root/'sessions'/aid/'logs').glob('*.jsonl'))
  return result
 def test_provider_non_json_stderr_is_preserved_and_redacted(self):
  x=self.bootstrap('error')['spool_text'];self.assertIn('Traceback: non-JSON bootstrap fixture',x);self.assertNotIn('fixture-NEVER-LOG',x);self.assertIn('[REDACTED]',x)
 def test_bootstrap_deadline_and_error_and_success(self):
  for m in ['hang','error','ready']:self.assertEqual(self.bootstrap(m)['event'],'secret_bootstrap_started')
if __name__=='__main__':unittest.main()

class FutureProtocol(unittest.TestCase):
 def worker(self):
  spec=importlib.util.spec_from_file_location('stream_worker',HERE/'claude-plugin/scripts/workerctl.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w);return w
 def test_early_log404_is_avoided_by_native_task_start_event(self):
  w=self.worker();calls=[];aid='11111111-1111-4111-8111-111111111111';sid='22222222-2222-4222-8222-222222222222'
  class API:
   def call(self,path,*a,**k):
    calls.append(path)
    if path.startswith('/v1/allocation/'):return {'ClientStatus':'running','TaskStates':{'fixed-prime':{'State':'pending' if calls.count(path)==1 else 'running'}}}
    raise AssertionError(path)
  def streams(api,path,deadline):
   if '/event/stream' in path:
    yield json.dumps({'Events':[{'Topic':'Allocation'}]}).encode()
   else:
    self.assertGreaterEqual(calls.count('/v1/allocation/'+aid),2,'No logs before task-start event')
    yield json.dumps({'event':'guest_session_ready','state':'ready'}).encode()
  with patch.object(w,'job_lifecycle',lambda *a:{'stop':False}),patch.object(w,'allocations',lambda *a:[{'ID':aid}]),patch.object(w,'session_access',lambda *a:{'allocation_id':aid,'session_id':sid}),patch.object(w,'checked_status',lambda *a:{'phase':'starting'}),patch.object(w,'stream_lines',streams):
   self.assertTrue(w.wait_session(API(),'legacy-worker/dispatch-123-test')['guest_ready'])
 def test_finish_does_not_destroy_vm_when_result_export_fails(self):
  w=self.worker();api=unittest.mock.Mock()
  with patch.object(w,'session_access',return_value={}),patch.object(w,'active_session',side_effect=TimeoutError()):
   with self.assertRaisesRegex(ValueError,'VM retained'):w.main(['finish','legacy-worker/dispatch-123-test'],api=api)
  api.call.assert_not_called()
 def test_workspace_copy_filters_binary_execbits_conflict_and_resume(self):
  w=self.worker()
  with tempfile.TemporaryDirectory() as src,tempfile.TemporaryDirectory() as dst:
   p=Path(src);(p/'script.sh').write_bytes(b'#!/bin/sh\necho fixture\n');(p/'script.sh').chmod(0o700);(p/'blob.bin').write_bytes(bytes(range(256))*400)
   (p/'.env').write_text('not-sent');(p/'output').mkdir();(p/'output'/'ignored').write_text('not-sent')
   root,files,excluded=w.workspace_manifest(src,['.']);self.assertEqual(len(files),2);self.assertGreaterEqual(excluded,1)
   destination='snapshots/test';transfer=hashlib.sha256(json.dumps({'destination':destination,'files':files},sort_keys=True,separators=(',',':')).encode()).hexdigest()
   def send(d):return art.operate({'transfer_id':transfer,**d},dst)
   send({'operation':'upload-start','destination':destination,'files':len(files),'bytes':sum(f['size'] for f in files)})
   for item in files:
    self.assertEqual(send({'operation':'upload-file',**item})['offset'],0);raw=(p/item['path']).read_bytes()
    for offset in range(0,len(raw),40000):
     d={'operation':'upload-chunk','path':item['path'],'offset':offset,'base64':base64.b64encode(raw[offset:offset+40000]).decode()};send(d);send(d)
   self.assertTrue(send({'operation':'upload-commit'})['committed']);self.assertTrue(send({'operation':'upload-commit'})['committed'])
   self.assertEqual((Path(dst)/destination/'blob.bin').read_bytes(),(p/'blob.bin').read_bytes());self.assertTrue((Path(dst)/destination/'script.sh').stat().st_mode&0o100)
   (Path(dst)/destination/'script.sh').write_text('existing changed file')
   with self.assertRaises(ValueError):send({'operation':'upload-commit'})
   (p/'link').symlink_to('/etc/hosts')
   with self.assertRaises(ValueError):w.workspace_manifest(src,['.'])

class CredentialDiagnostics(unittest.TestCase):
 def test_transient_read_retry_no_secret_in_error_and_denial_no_retry(self):
  secret='a'*64
  timeout=subprocess.CompletedProcess([],78,b'',b"bao_exit=1 http_codes=[] classes=['i/o timeout']\nprovider_stage=selected_field exception=ValueError")
  ok=subprocess.CompletedProcess([],0,secret.encode(),b'')
  with patch.object(acp_bridge.subprocess,'run',side_effect=[timeout,ok]) as run,patch.object(acp_bridge.time,'sleep'):
   self.assertEqual(acp_bridge.selected_rest_key({'rest_key_command':['fixed-provider']}),secret);self.assertEqual(run.call_count,2)
  denied=subprocess.CompletedProcess([],78,b'',b"bao_exit=2 http_codes=['403'] classes=['permission denied']")
  with patch.object(acp_bridge.subprocess,'run',return_value=denied) as run:
   with self.assertRaisesRegex(RuntimeError,'403') as err:acp_bridge.selected_rest_key({'rest_key_command':['fixed-provider']})
   self.assertEqual(run.call_count,1);self.assertNotIn(secret,str(err.exception))
