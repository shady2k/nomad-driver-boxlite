import test_support
import unittest,json,io,hashlib
from unittest.mock import patch
from test_project_cli import w
class OCIPlatform(unittest.TestCase):
 def fixture(self,corrupt=False,duplicate=False,config_arch='amd64'):
  values={}
  def blob(v):
   b=json.dumps(v,separators=(',',':')).encode();d='sha256:'+hashlib.sha256(b).hexdigest();values[d]=b;return d
  cfg=blob({'os':'linux','architecture':config_arch});amd=blob({'schemaVersion':2,'config':{'digest':cfg}});armcfg=blob({'os':'linux','architecture':'arm64'});arm=blob({'schemaVersion':2,'config':{'digest':armcfg}});ds=[{'platform':{'os':'linux','architecture':'amd64'},'digest':amd},{'platform':{'os':'linux','architecture':'arm64'},'digest':arm}]
  if duplicate:ds.append(ds[0])
  index=blob({'schemaVersion':2,'manifests':ds})
  def read(req,timeout):
   d=req.full_url.rsplit('/',1)[-1];return io.BytesIO(b'{}' if corrupt else values[d])
  return index,amd,arm,read
 def test_index_selects_actual_matching_child_without_pinning_node(self):
  index,amd,arm,read=self.fixture()
  with patch.object(w.urllib.request,'urlopen',read):
   for arch,d in [('amd64',amd),('arm64',arm)]:
    r=w.resolve_platform_image('registry.example/image@'+index,arch);self.assertTrue(r['resolved_image'].endswith('@'+d));self.assertEqual(r['index_digest'],index)
 def test_missing_duplicate_corrupt_and_descriptor_config_mismatch_fail_closed(self):
  for kwargs in [{'corrupt':True},{'duplicate':True},{'config_arch':'arm64'}]:
   index,_,_,read=self.fixture(**kwargs)
   with patch.object(w.urllib.request,'urlopen',read),self.assertRaises(ValueError):w.resolve_platform_image('registry.example/image@'+index,'amd64')
 def test_single_arch_digest_stays_immutable_and_wrong_arch_rejected(self):
  _,amd,_,read=self.fixture()
  with patch.object(w.urllib.request,'urlopen',read):
   r=w.resolve_platform_image('registry.example/image@'+amd,'amd64');self.assertIsNone(r['index_digest']);self.assertEqual(r['platform_digest'],amd)
   with self.assertRaises(ValueError):w.resolve_platform_image('registry.example/image@'+amd,'arm64')
if __name__=='__main__':unittest.main()

class OCIIndexDiscovery(unittest.TestCase):
 def test_discovery_exposes_both_platforms_and_selected_child_tools(self):
  values={}
  def blob(v):
   b=json.dumps(v).encode();d='sha256:'+hashlib.sha256(b).hexdigest();values[d]=b;return d
  children=[]
  for arch in ['amd64','arm64']:
   cfg=blob({'os':'linux','architecture':arch,'config':{'Labels':{'io.boxlite.worker-contract':'2','io.boxlite.tool.go.version':'1.26.5'}}});child=blob({'schemaVersion':2,'config':{'digest':cfg}});children.append({'platform':{'os':'linux','architecture':arch},'digest':child})
  index=blob({'schemaVersion':2,'manifests':children})
  class Response(io.BytesIO):
   def __init__(self,b,d):super().__init__(b);self.headers={'Docker-Content-Digest':d}
  def read(req,timeout):
   u=req.full_url
   if '_catalog' in u:return Response(json.dumps({'repositories':['worker']}).encode(),None)
   if 'tags/list' in u:return Response(json.dumps({'tags':['v1']}).encode(),None)
   d=u.rsplit('/',1)[-1];d=index if d=='v1' else d
   return Response(values[d],d)
  with patch.object(w.urllib.request,'urlopen',read):
   r=w.registry_images('https://registry.example',workers_only=True,architecture='amd64')
  row=r['images'][0];self.assertEqual({p['architecture'] for p in row['platforms']},{'amd64','arm64'});self.assertEqual(row['resolved_digest'],children[0]['digest']);self.assertEqual(row['tool_metadata']['tools'],{'go':'1.26.5'});self.assertEqual(r['compatible_worker_count'],1)
