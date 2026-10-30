import test_support
import copy,json,sys,tempfile,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import universal,contract,dispatcher_auth
class API:
 def __init__(self):self.calls=[];self.rows=[];self.jobs={};self.fail=False
 def call(self,path,method='GET',body=None):
  self.calls.append((path,method,body))
  if path.startswith('/v1/jobs?'):return self.rows
  if path.startswith('/v1/allocations?'):return getattr(self,'allocs',[])
  if method=='GET':return self.jobs[path.split('/v1/job/')[1].split('?')[0]]
  if self.fail:raise TimeoutError()
  if path=='/v1/jobs':self.job=body['Job'];return {}
  return {'DispatchedJobID':self.job['ID']+'/dispatch-1-abcd'}
NODE='00000000-0000-4000-8000-000000000001'
BUDGET={'max_vms':4,'max_guest_cpus':8,'max_reservation_mib':24576,'memory_overhead_mib':1024,'cpu_mhz_per_vcpu':3000,'default_disk_gib':12,'max_disk_gib':12}
def variable():return {'Items':{'boxlite_node_budgets':json.dumps({'version':1,'nodes':{NODE:BUDGET}})},'ModifyIndex':42}
def catalog():return universal.parse_catalog(variable())
def req(**kw):return dict(node_id=NODE,disk_gib=12,image='registry.example/image@sha256:'+'a'*64,cpus=1,memory_mib=512,request_id=str(uuid.uuid4()),session_id=str(uuid.uuid4()),**kw)
class Universal(unittest.TestCase):
 def test_project_display_preserves_immutable_identity(self):
  d=req();a=universal.build_job(d,catalog());b=universal.build_job(d,catalog(),'Project Alpha')
  self.assertEqual(b['Name'],'project-alpha-'+a['ID']);self.assertEqual(a['ID'],b['ID'])
  self.assertEqual(a['TaskGroups'],b['TaskGroups']);self.assertEqual(a['Meta'],b['Meta'])
  self.assertEqual(universal.verify_job(d,b,catalog()),universal.identity(d))
  d2=req();self.assertNotEqual(universal.build_job(d2,catalog(),'Project Alpha')['Name'],b['Name'])
 def test_project_invalid_and_legacy(self):
  d=req();self.assertEqual(universal.build_job(d,catalog())['Name'],universal.parent(d))
  for text in ['', '   ','---','https://example.test/a','/home/user/repo','x'*65,'a\nsecret']:
   with self.assertRaises(ValueError):universal.build_job(d,catalog(),text)
  self.assertTrue(universal.is_parent(universal.parent(d)))
  self.assertFalse(universal.is_parent('project-alpha-'+universal.parent(d)))
 def test_operator_host_network_is_not_a_request_default_or_identity(self):
  from unittest.mock import patch
  d=req();identity=universal.identity(d)
  with patch.dict(contract._pm.CONFIG,{'ports':{'min':20000,'max':20010}}):
   with self.assertRaisesRegex(ValueError,'operator host network'):universal.build_job(d,catalog())
  self.assertEqual(universal.identity(d),identity)
 def test_explicit_resources(self):
  for cpu,mem in [(1,512),(2,3072),(3,6144),(4,8192)]:
   d=req();d.update(cpus=cpu,memory_mib=mem);j=universal.build_job(d,catalog());ts=j['TaskGroups'][0]['Tasks']
   self.assertEqual(ts[0]['Config']['guest_cpus'],cpu);self.assertEqual(sum(t['Resources']['MemoryMB'] for t in ts),mem+1024)
   self.assertEqual(sum(t['Resources']['CPU'] for t in ts),cpu*3000);self.assertGreaterEqual(ts[0]['Resources']['CPU'],cpu*2950)
   self.assertEqual(len(ts),1);self.assertNotIn('Templates',ts[0]);self.assertNotIn('command',ts[0]['Config']);self.assertEqual(universal.verify_job(d,j,catalog()),universal.identity(d))
 def test_disk_40_uses_nomad_group_reservation(self):
  c=catalog();c['nodes'][NODE]['max_disk_gib']=40
  d=req();d['disk_gib']=40;j=universal.build_job(d,c)
  self.assertEqual(j['TaskGroups'][0]['EphemeralDisk'],{'SizeMB':41984,'Sticky':False,'Migrate':False})
  d['disk_gib']=41
  with self.assertRaises(ValueError):universal.build_job(d,c)
 def test_image_capabilities_use_nomad_constraints_without_caller_profile(self):
  j=universal.build_job(req(),catalog(),required_guest_features=['docker','tun'])
  self.assertIn({'LTarget':'${attr.driver.boxlite.guest.docker}','Operand':'=','RTarget':'true'},j['Constraints'])
  self.assertIn({'LTarget':'${attr.driver.boxlite.guest.tun}','Operand':'=','RTarget':'true'},j['Constraints'])
  with self.assertRaises(ValueError):universal.build_job(req(),catalog(),required_guest_features=['host-mount'])
 def test_no_profiles_or_jobspec(self):
  for k,v in [('profile','legacy-small'),('Tasks',[]),('host','127.0.0.1'),('mounts',[]),('command',['sh']),('network',{})]:
   d=req();d[k]=v
   with self.assertRaises(ValueError):universal.validate(d)
 def test_invalid_types(self):
  for k,v in [('cpus',True),('cpus',0),('memory_mib',-1),('image','test:latest'),('disk_gib',0)]:
   d=req();d[k]=v
   with self.assertRaises(ValueError):universal.validate(d)
 def test_identity_normalized_and_immutable(self):
  d=universal.validate(req());self.assertEqual(universal.identity(d),universal.identity(dict(reversed(list(d.items())))))
  for k,v in [('image','registry.example/b@sha256:'+'b'*64),('cpus',2),('memory_mib',1024),('session_id',str(uuid.uuid4()))]:
   x=copy.deepcopy(d);x[k]=v;self.assertNotEqual(universal.identity(d),universal.identity(x))
 def test_auth_domain_is_spec_bound(self):
  d=req();a=universal.identity(d);d['cpus']=2;b=universal.identity(d)
  self.assertNotEqual(dispatcher_auth.derive(b'x'*32,d['session_id'],a),dispatcher_auth.derive(b'x'*32,d['session_id'],b))
 def test_template_substitution_rejected(self):
  d=req()
  for field,value in [('Driver','raw_exec'),('Config',{'command':'sh'}),('Env',{'SECRET':'x'}),('Resources',{'CPU':1,'MemoryMB':1})]:
   j=universal.build_job(d,catalog());j['TaskGroups'][0]['Tasks'][0][field]=value
   with self.assertRaises(ValueError):universal.verify_job(d,j,catalog())
 def test_policy_is_authoritative_not_local_defaults(self):
  v=variable();p=json.loads(v['Items'][universal.POLICY_ITEM]);p['nodes'][NODE]['max_guest_cpus']=2;v['Items'][universal.POLICY_ITEM]=json.dumps(p)
  d=req();d['cpus']=3
  with self.assertRaises(ValueError):universal.build_job(d,universal.parse_catalog(v))
  for value in [0,-1]:
   p['nodes'][NODE]['max_guest_cpus']=value;v['Items'][universal.POLICY_ITEM]=json.dumps(p)
   with self.assertRaises(ValueError):universal.parse_catalog(v)
  with self.assertRaises(ValueError):universal.parse_catalog({'Items':{},'ModifyIndex':1})
  self.assertFalse(hasattr(universal,'LIMITS'))
 def test_direct_jobs_have_no_host_execution_surfaces(self):
  j=universal.build_job(req(),catalog());ts=j['TaskGroups'][0]['Tasks'];self.assertEqual(len(ts),1)
  self.assertEqual(ts[0]['Driver'],'boxlite');self.assertTrue(all(k not in ts[0] for k in ['Templates','Artifacts','Env','User','Vault']))
  self.assertNotIn('ParameterizedJob',j);self.assertNotIn('Payload',j)
 def test_legacy_compatibility(self):
  d={'profile':'legacy-small','request_id':str(uuid.uuid4()),'session_id':str(uuid.uuid4())};self.assertEqual(contract.validate(d)['profile'],'legacy-small')
if __name__=='__main__':unittest.main()
