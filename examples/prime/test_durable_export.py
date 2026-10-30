import unittest,tempfile,os,json,hashlib,concurrent.futures
from pathlib import Path
from unittest.mock import patch
import test_support,sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'claude-plugin/scripts'))
import durable_export as d
JOB='worker-11111111-1111-4111-8111-111111111111';SID='22222222-2222-4222-8222-222222222222';RID='33333333-3333-4333-8333-333333333333'
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.patch=patch.object(Path,'home',return_value=self.home);self.patch.start();self.root=d.base();self.root.mkdir(parents=True,mode=0o700);self.root.chmod(0o700)
  self.info={'session_id':SID,'state':'ready','request_count':1,'requests':[{'request_id':RID,'state':'completed'}]};self.full='.nomad-results/'+RID+'.json';self.paths=[self.full,'result.bundle'];self.art=self.root/'artifacts'/SID/'fixture';self.art.mkdir(parents=True,mode=0o700);self.manifest=self.art/'manifest.json'
  self.write(self.root/'results'/(RID+'.json'),{'job':JOB,'result':{'session_id':SID,'request_id':RID,'state':'completed','result_file':self.full}})
  files=[]
  for name,raw in [(self.full,b'full result'),('result.bundle',b'synthetic bundle')]:
   p=self.art/'files'/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);p.write_bytes(raw);p.chmod(0o600);files.append({'path':name,'local_path':str(p),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
  self.write(self.manifest,{'job':JOB,'session_id':SID,'artifacts':files})
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def write(self,p,obj):p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);p.write_text(json.dumps(obj));p.chmod(0o600)
 def ack(self,**kw):return d.acknowledge(JOB,kw.get('info',self.info),RID,self.manifest,kw.get('paths',self.paths),kw.get('complete',True))
 def track(self):self.write(self.root/'receipts'/(JOB[7:]+'.json'),{'state':'dispatched','response':{'DispatchedJobID':JOB,'StateContractVersion':1}})
 def test_legacy_ready_is_unknown(self):self.assertEqual(d.classify(JOB,self.info)['state'],'unknown')
 def test_new_ready_without_ack_is_unexported(self):self.track();self.assertEqual(d.classify(JOB,self.info)['state'],'ready-unexported')
 def test_download_does_not_auto_ack(self):self.track();self.assertFalse((self.root/'exports').exists());self.assertEqual(d.classify(JOB,self.info)['state'],'ready-unexported')
 def test_verified_atomic_ack_idempotent_and_exported(self):
  a=self.ack();self.assertEqual(a['storage_kind'],'coordinator-host');self.assertFalse(a['replicated']);self.assertFalse(a['auto_close_enabled']);self.assertTrue(Path(a['commit_path']).exists());self.assertTrue(self.ack()['idempotent']);self.assertEqual(d.classify(JOB,self.info)['state'],'ready-exported')
 def test_single_file_or_implicit_declaration_not_enough(self):
  with self.assertRaises(ValueError):self.ack(paths=['result.bundle'])
  with self.assertRaises(ValueError):self.ack(complete=False)
 def test_busy_multi_day_never_idle_or_acked(self):
  info={**self.info,'state':'busy','active_request_id':RID,'running_elapsed_ms':9*86400000};self.assertEqual(d.classify(JOB,info)['state'],'busy')
  with self.assertRaises(ValueError):self.ack(info=info)
 def test_explicit_question_is_awaiting_input_not_idle(self):self.assertEqual(d.classify(JOB,{**self.info,'questions':[{'state':'pending','message':'PRIVATE'}]})['state'],'awaiting-input')
 def test_unknown_or_incomplete_is_not_exported(self):
  self.ack();self.assertEqual(d.classify(JOB,{**self.info,'state':'unknown'})['state'],'unknown');self.assertEqual(d.classify(JOB,{**self.info,'requests_truncated':True})['state'],'unknown')
 def test_new_terminal_request_invalidates_all_exported_state(self):
  self.track();self.ack();info={**self.info,'request_count':2,'requests':self.info['requests']+[{'request_id':'44444444-4444-4444-8444-444444444444','state':'completed'}]};self.assertEqual(d.classify(JOB,info)['state'],'ready-unexported')
 def test_symlink_and_tamper_rejected(self):
  p=self.art/'files/result.bundle';p.unlink();p.symlink_to(self.root/'results'/(RID+'.json'))
  with self.assertRaises(OSError):self.ack()
 def test_commit_invalidated_by_file_change(self):
  self.track();self.ack();p=self.art/'files/result.bundle';p.write_bytes(b'changed');self.assertEqual(d.classify(JOB,self.info)['state'],'ready-unexported')
 def test_partial_commit_is_not_ack(self):
  self.track();p=self.root/'exports'/SID/RID;p.mkdir(parents=True,mode=0o700);self.write(p/'manifest.json',{});self.assertEqual(d.classify(JOB,self.info)['state'],'ready-unexported')
 def test_secret_paths_denied(self):
  for p in ['.env','.ssh/id_ed25519','secret.key','../escape']:
   with self.assertRaises(ValueError):d.relative(p)
 def test_concurrent_commit_no_duplicate(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:self.ack(),range(2)))
  self.assertEqual(rows[0]['manifest_sha256'],rows[1]['manifest_sha256'])
if __name__=='__main__':unittest.main()
