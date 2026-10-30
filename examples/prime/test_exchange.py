import unittest,sys,tempfile,uuid,json,hashlib,datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent));import exchange as e,exchange_bridge as b
CFG={'endpoint':'https://s3.example.test','bucket':'exchange-test','region':'us-east-1'}
JOB='worker-'+str(uuid.uuid4());SID=str(uuid.uuid4());RID=str(uuid.uuid4());RAW=b'synthetic\0binary';SHA=hashlib.sha256(RAW).hexdigest();FILES=[{'path':'out.bin','size':len(RAW),'sha256':SHA}]
class Memory(e.Store):
 def __init__(self):self.cfg=CFG;self.key=('id','secret');self.objects={}
 def put(self,k,v):self.objects[k]=v
 def get(self,k,limit=1048576):
  if k not in self.objects:raise e.ExchangeError('missing')
  return self.objects[k]
 def verify(self,k,size,sha):
  raw=self.get(k)
  if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=sha:raise e.ExchangeError('hash')
class Tests(unittest.TestCase):
 def test_sign_exact(self):
  s=e.Store(CFG,('ID','SECRET'));u=s.url('PUT','jobs/test/file',300,datetime.datetime(2026,1,1,tzinfo=datetime.timezone.utc));self.assertIn('X-Amz-Expires=300',u);self.assertNotIn('SECRET',u);self.assertNotEqual(u,s.url('GET','jobs/test/file',300,datetime.datetime(2026,1,1,tzinfo=datetime.timezone.utc)))
 def test_expiry_bound(self):
  for n in [0,-1,901]:
   with self.assertRaises(e.ExchangeError):e.Store(CFG,('i','s')).url('PUT','a',n)
 def test_sign_verb(self):
  with self.assertRaises(e.ExchangeError):e.Store(CFG,('i','s')).url('PATCH','a')
 def test_paths(self):
  for p in ['../x','/x','.env','.ssh/key','a/.git/x','a//x','a.pem']:
   with self.assertRaises(e.ExchangeError):e.path(p)
 def test_identity(self):
  self.assertNotEqual(e.identity(JOB,SID,RID),e.identity(JOB,str(uuid.uuid4()),RID))
 def test_quota(self):
  with self.assertRaises(e.ExchangeError):e.files([{'path':'x','size':268435457,'sha256':SHA}])
 def test_duplicate(self):
  with self.assertRaises(e.ExchangeError):e.files(FILES+FILES)
 def test_commit(self):
  s=Memory();s.put(e.identity(JOB,SID,RID)+'objects/'+SHA,RAW);a=s.commit(JOB,SID,RID,FILES);self.assertEqual(a,s.commit(JOB,SID,RID,FILES));self.assertFalse(a['auto_close_enabled']);self.assertEqual(s.manifest(JOB,SID,RID,a['manifest_sha256'])['files'][0]['sha256'],SHA)
 def test_partial_no_commit(self):
  s=Memory()
  with self.assertRaises(e.ExchangeError):s.commit(JOB,SID,RID,FILES)
  self.assertFalse(any('/commits/' in k for k in s.objects))
 def test_mismatch(self):
  s=Memory();s.put(e.identity(JOB,SID,RID)+'objects/'+SHA,b'bad')
  with self.assertRaises(e.ExchangeError):s.commit(JOB,SID,RID,FILES)
 def test_cross_session(self):
  s=Memory();s.put(e.identity(JOB,SID,RID)+'objects/'+SHA,RAW);a=s.commit(JOB,SID,RID,FILES)
  with self.assertRaises(e.ExchangeError):s.manifest(JOB,str(uuid.uuid4()),RID,a['manifest_sha256'])
 def test_missing_config_never_reads_credentials(self):
  with tempfile.TemporaryDirectory() as t,patch.object(e,'credentials',side_effect=AssertionError):
   call=lambda *a:(200,b'{"state":"ready"}')
   bridge=b.Bridge(call,JOB,SID,Path(t)/'absent');code,raw=bridge('/status','GET',b'');self.assertEqual(code,200);self.assertFalse(json.loads(raw)['capabilities']['s3_exchange']);self.assertEqual(bridge('/exchange','POST',b'{}')[0],409);self.assertEqual(bridge('/prompt','POST',b'{}'),call())
 def test_configured_unreachable_no_success(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config';p.write_text('{}')
   with patch.object(e,'config',return_value=CFG),patch.object(e,'credentials',side_effect=e.ExchangeError('exchange_bao_unavailable')):
    code,raw=b.Bridge(lambda *a:None,JOB,SID,p)('/exchange','POST',json.dumps({'request_id':RID,'operation':'input-plan'}).encode());self.assertEqual(code,503);self.assertNotIn('manifest_sha256',json.loads(raw));self.assertFalse(json.loads(raw)['retry_prompt'])
 def test_unknown_operation(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config';p.write_text('{}')
   with patch.object(e,'config',return_value=CFG),patch.object(e,'Store',return_value=Memory()):self.assertEqual(b.Bridge(lambda *a:None,JOB,SID,p)('/exchange','POST',json.dumps({'request_id':RID,'operation':'evil'}).encode())[0],409)
if __name__=='__main__':unittest.main()

class LifecycleTests(unittest.TestCase):
 def bridge(self,folder,state='ready',terminal=True):
  store=Memory();prefix=e.identity(JOB,SID,RID);raws={'result.bin':RAW,'.nomad-results/'+RID+'.json':b'{"synthetic":true}'}
  def call(url,method,body):
   if url=='/status':return 200,json.dumps({'session_id':SID,'state':state,'active_request_id':RID if state=='busy' else None,'requests':[{'request_id':RID,'state':'completed'}] if terminal else []}).encode()
   d=json.loads(body)
   if d['operation']=='manifest':return 200,json.dumps({'files':[{'path':p,'size':len(raws[p]),'sha256':hashlib.sha256(raws[p]).hexdigest()} for p in d['paths']]}).encode()
   if d['operation']=='s3-push':store.put(prefix+'objects/'+d['sha256'],raws[d['path']]);return 200,b'{"transferred":true}'
   raise AssertionError('unexpected operation')
  p=Path(folder)/'config';p.write_text('{}');return b.Bridge(call,JOB,SID,p),store
 def test_terminal_full_result_commit(self):
  with tempfile.TemporaryDirectory() as t:
   bridge,store=self.bridge(t)
   with patch.object(e,'config',return_value=CFG),patch.object(e,'Store',return_value=store):code,raw=bridge('/exchange','POST',json.dumps({'operation':'output-export','request_id':RID,'paths':['result.bin','.nomad-results/'+RID+'.json'],'all_outputs_declared':True}).encode())
   self.assertEqual(code,200);self.assertEqual(json.loads(raw)['scope'],'declared-terminal-results')
 def test_busy_cannot_export_or_commit(self):
  with tempfile.TemporaryDirectory() as t:
   bridge,store=self.bridge(t,state='busy')
   with patch.object(e,'config',return_value=CFG),patch.object(e,'Store',return_value=store):code,raw=bridge('/exchange','POST',json.dumps({'operation':'snapshot-export','request_id':RID,'paths':['result.bin'],'all_outputs_declared':True}).encode())
   self.assertEqual(code,409);self.assertFalse(store.objects)
 def test_single_file_not_terminal_full_export(self):
  with tempfile.TemporaryDirectory() as t:
   bridge,store=self.bridge(t)
   with patch.object(e,'config',return_value=CFG),patch.object(e,'Store',return_value=store):code,raw=bridge('/exchange','POST',json.dumps({'operation':'output-export','request_id':RID,'paths':['result.bin'],'all_outputs_declared':True}).encode())
   self.assertEqual(code,409);self.assertFalse(store.objects)
