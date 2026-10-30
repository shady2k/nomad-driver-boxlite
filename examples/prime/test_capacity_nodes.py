import test_support
import unittest
from unittest.mock import patch
from test_project_cli import w
from test_universal import catalog,NODE,BUDGET
OTHER='00000000-0000-4000-8000-000000000002'
class API:
 def call(self,path,*args):
  if path=='/v1/allocations':return []
  raise AssertionError('Unexpected API: '+path)
class CapacityNodes(unittest.TestCase):
 def test_per_node_architecture_and_actual_scheduler_read(self):
  c=catalog();c['nodes'][NODE]={**BUDGET,'max_vms':4};c['nodes'][OTHER]={**BUDGET,'max_vms':8}
  inv={'complete':True,'nodes':[{'id':NODE,'name':'node-arm','host_architecture':'arm64','host_kernel':'darwin','resources':{'Memory':{'MemoryMB':49152},'Cpu':{'CpuShares':55000}},'reserved':{'Memory':{'MemoryMB':19456},'Cpu':{'CpuShares':31000}}},{'id':OTHER,'name':'node-x86','host_architecture':'amd64','host_kernel':'linux','resources':{'Memory':{'MemoryMB':32000},'Cpu':{'CpuShares':16000}},'reserved':{'Memory':{'MemoryMB':2048},'Cpu':{'CpuShares':500}}}]}
  with patch.object(w,'policy_catalog',return_value=c),patch.object(w,'node_inventory',return_value=inv):r=w.capacity_summary(API())
  a=r['nodes'][NODE];b=r['nodes'][OTHER]
  self.assertEqual(a['scope'],'per_node_not_cluster');self.assertEqual(a['host_architecture'],'arm64');self.assertEqual(b['host_architecture'],'amd64');self.assertEqual(a['limits']['max_vms'],4);self.assertEqual(b['limits']['max_vms'],8);self.assertEqual(b['scheduler_capacity']['allocatable_cpu_mhz'],15500);self.assertTrue(a['scheduler_capacity']['available']);self.assertNotIn('global_vm_limit',r);self.assertFalse(r['completeness']['global_free_capacity_confirmed'])
 def test_denial_is_observed_not_assumed_and_not_global_free(self):
  inv={'complete':False,'nodes':[],'phase':'node_inventory_denied','required_scope':'node:read'}
  with patch.object(w,'policy_catalog',return_value=catalog()),patch.object(w,'node_inventory',return_value=inv):r=w.capacity_summary(API())
  s=r['nodes'][NODE]['scheduler_capacity'];self.assertFalse(s['available']);self.assertEqual(s['reason'],'node_inventory_denied');self.assertEqual(s['required_scope'],'node:read')
if __name__=='__main__':unittest.main()
