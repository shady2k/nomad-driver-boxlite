import test_support
import copy,json,unittest,tempfile,contextlib,io,platform
from pathlib import Path
from unittest.mock import patch
import universal,companion
from test_universal import req,catalog,NODE,BUDGET
from test_project_cli import w,API
OTHER='00000000-0000-4000-8000-000000000002'
def automatic(arch='amd64'):
 d=req();d.pop('node_id');d['architecture']=arch;return d
class Placement(unittest.TestCase):
 def test_nomad_selects_node_without_pinning_host_os(self):
  c=catalog();c['nodes'][OTHER]=dict(BUDGET)
  for arch in ['amd64','arm64']:
   d=automatic(arch);j=universal.build_job(d,c)
   self.assertFalse(any(x['LTarget']=='${node.unique.id}' for x in j['Constraints']))
   self.assertIn({'LTarget':'${attr.cpu.arch}','Operand':'=','RTarget':arch},j['Constraints'])
   self.assertFalse(any('kernel.name' in x['LTarget'] for x in j['Constraints']))
   self.assertEqual(universal.verify_job(d,j,c),universal.identity(d))
 def test_reservation_covers_every_registered_node_conversion(self):
  c=catalog();c['nodes'][OTHER]={**BUDGET,'cpu_mhz_per_vcpu':4000,'memory_overhead_mib':2048}
  d=automatic();j=universal.build_job(d,c);r=j['TaskGroups'][0]['Tasks'][0]['Resources']
  self.assertEqual(r,{'CPU':4000,'MemoryMB':2560})
  self.assertNotIn('node_id',json.loads(j['TaskGroups'][0]['Tasks'][0]['Config']['worker_request']))
 def test_budget_cannot_combine_unrelated_maxima_to_admit_impossible_request(self):
  c=catalog();c['nodes'][NODE]={**BUDGET,'max_guest_cpus':1};c['nodes'][OTHER]={**BUDGET,'max_reservation_mib':2048}
  d=automatic();d.update(cpus=4,memory_mib=4096)
  with self.assertRaisesRegex(ValueError,'every applicable'):universal.build_job(d,c)
 def test_legacy_pin_and_identity_unchanged(self):
  d=req();self.assertNotIn('architecture',universal.validate(d));j=universal.build_job(d,catalog());self.assertEqual(j['Constraints'][0]['RTarget'],NODE)
  a=universal.identity(d);d['architecture']='arm64';self.assertNotEqual(a,universal.identity(d))
 def test_unknown_arch_and_missing_arch_fail_closed(self):
  for a in ['',None,'x86_64','linux','ARM64']:
   d=automatic();d['architecture']=a
   with self.assertRaises(ValueError):universal.validate(d)
  d=automatic();d.pop('architecture')
  with self.assertRaisesRegex(ValueError,'no default NodeID'):universal.validate(d)
 def test_cli_omitted_node_is_not_filled_from_single_node_catalog(self):
  api=API();policy={'provider':'fixture','default_model':'example-model','allowed_models':['example-model']}
  with tempfile.TemporaryDirectory() as td,patch.object(w.Path,'home',return_value=Path(td)),patch.object(w,'policy_catalog',return_value=catalog()),patch.object(w.auth,'protected_json',return_value=policy),patch.object(w.auth,'access',return_value={'token':'synthetic'}),contextlib.redirect_stdout(io.StringIO()):
   r=w.main(['request','--architecture','arm64','--image','registry.example/image@sha256:'+'a'*64,'--cpus','1','--memory-mib','512'],api=api)
   payload=json.loads(api.job['TaskGroups'][0]['Tasks'][0]['Config']['worker_request']);self.assertNotIn('node_id',payload);self.assertEqual(payload['architecture'],'arm64');self.assertEqual(sum(m=='POST' for _,m,_ in api.calls),1)
 def test_arch_admission_uses_real_node_and_host(self):
  d=automatic('amd64');e={'request':json.dumps(d),'namespace':'boxlite-prime','task_name':'vm','job_id':universal.parent(d),'node_id':OTHER,'logical_id':d['session_id'],'image':d['image'],'cpus':d['cpus'],'memory_mib':d['memory_mib'],'disk_gib':d['disk_gib'],'port':20000,'host':'192.0.2.20','allocation_id':NODE}
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/'guest').mkdir()
   for f,v in [(root/'node.json',{'lan_ip':e['host']}),(root/'guest/session-policy.json',{'provider':'fixture','default_model':'example-model','allowed_models':['example-model']})]:f.write_text(json.dumps(v));f.chmod(0o600)
   with patch.object(companion,'ROOT',root),patch.object(companion,'HERE',root),patch.object(companion.platform,'machine',return_value='x86_64'):self.assertEqual(companion.validate(e)[0],universal.validate(d))
   with patch.object(companion,'ROOT',root),patch.object(companion,'HERE',root),patch.object(companion.platform,'machine',return_value='arm64'):
    with self.assertRaisesRegex(ValueError,'host architecture'):companion.validate(e)
class Inventory(unittest.TestCase):
 def test_details_are_authoritative_not_summary_architecture(self):
  class API:
   def __init__(self):self.paths=[]
   def call(self,path):
    self.paths.append(path)
    if path=='/v1/nodes?resources=true':return [{'ID':NODE}]
    return {'ID':NODE,'Name':'example-worker','Status':'ready','SchedulingEligibility':'eligible','Attributes':{'cpu.arch':'arm64','kernel.name':'darwin','driver.boxlite.api':'v1'}}
  api=API();r=w.node_inventory(api);self.assertTrue(r['complete']);self.assertEqual(r['nodes'][0]['host_architecture'],'arm64');self.assertEqual(r['nodes'][0]['host_kernel'],'darwin');self.assertEqual(api.paths,['/v1/nodes?resources=true','/v1/node/'+NODE])
 def test_denied_inventory_is_incomplete_not_free_capacity(self):
  import urllib.error
  class API:
   def call(self,path):raise urllib.error.HTTPError('https://nomad.example',403,'denied',{},None)
  r=w.node_inventory(API());self.assertFalse(r['complete']);self.assertEqual(r['phase'],'node_inventory_denied');self.assertFalse(r['credentials_changed'])

 def test_inventory_has_bounded_deadline_and_restores_outer_wait(self):
  import time
  class API:
   wait_deadline=time.monotonic()+60
   def call(self,path):
    self.observed_deadline=self.wait_deadline
    return []
  api=API();original=api.wait_deadline;r=w.node_inventory(api)
  self.assertTrue(r['complete']);self.assertEqual(api.wait_deadline,original);self.assertLess(api.observed_deadline,original)
 def test_inventory_transport_error_is_unknown_not_empty_free_capacity(self):
  class API:
   def call(self,path):raise TimeoutError()
  r=w.node_inventory(API());self.assertFalse(r['complete']);self.assertEqual(r['phase'],'node_inventory_unavailable')

class ArchitectureAccounting(unittest.TestCase):
 def mapped(self):
  c=catalog();c['nodes'][OTHER]={**BUDGET,'cpu_mhz_per_vcpu':1900,'max_reservation_mib':30053}
  def row(id,arch):return {'id':id,'host_architecture':arch,'datacenter':'dc1','node_pool':'default','boxlite_api':'v1'}
  c['placement_inventory']={'complete':True,'nodes':[row(NODE,'arm64'),row(OTHER,'amd64')]};return c
 def test_amd64_uses_real_architecture_without_node_pin(self):
  c=self.mapped();d=automatic();d['cpus']=8;j=universal.build_job(d,c);self.assertEqual(j['TaskGroups'][0]['Tasks'][0]['Resources']['CPU'],15200)
  self.assertFalse(any(x['LTarget']=='${node.unique.id}' for x in j['Constraints']))
  self.assertEqual(universal.verify_job(d,j,c),universal.identity(d))
 def test_same_architecture_uses_largest_policy_conversion(self):
  c=self.mapped();c['placement_inventory']['nodes'][0]['host_architecture']='amd64'
  self.assertEqual(universal.request_policy(c,architecture='amd64')['cpu_mhz_per_vcpu'],3000)
 def test_legacy_and_incomplete_inventory_are_conservative(self):
  for inv in [None,{'complete':False,'phase':'node_inventory_denied','nodes':[]},{'complete':True,'nodes':[]}]:
   c=self.mapped()
   if inv is None:c.pop('placement_inventory')
   else:c['placement_inventory']=inv
   self.assertEqual(universal.request_policy(c,architecture='amd64')['cpu_mhz_per_vcpu'],3000)
 def test_datacenter_pool_and_driver_constraints_match(self):
  for key,value in [('datacenter','other'),('node_pool','other'),('boxlite_api','unknown')]:
   c=self.mapped();c['placement_inventory']['nodes'][1][key]=value
   with self.assertRaisesRegex(ValueError,'compatible Nomad node'):universal.build_job(automatic('amd64'),c)
 def test_actual_resource_availability_is_not_a_custom_scheduler(self):
  c=self.mapped();c['placement_inventory']['nodes'][1].update({'status':'down','scheduling_eligibility':'ineligible','resources':{'Cpu':{'CpuShares':1}}})
  self.assertEqual(universal.request_policy(c,architecture='amd64')['cpu_mhz_per_vcpu'],1900)

if __name__=='__main__':unittest.main()
