import hashlib,importlib.util,os,runpy,shutil,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('runtime_paths_test',REPO/'examples/prime/node_paths.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class RuntimeAssets(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.assets=self.root/'assets';self.assets.mkdir(mode=0o700);self.files={}
  for n in ['boxlite-shim','boxlite-guest','bwrap','libkrunfw.so.5']:
   p=self.assets/n;p.write_bytes(('fixture-'+n).encode());p.chmod(0o555);self.files[n]=hashlib.sha256(p.read_bytes()).hexdigest()
  self.selected={'runtime_assets':{'directory':str(self.assets),'files':self.files}}
 def tearDown(self):self.tmp.cleanup()
 def test_absent_reads_no_assets(self):
  with patch.object(m.os,'open',side_effect=AssertionError('unexpected read')):self.assertIsNone(m.trusted_runtime({}))
 def test_verified(self):self.assertEqual(m.trusted_runtime(self.selected,'Linux'),str(self.assets))
 def test_mutation_rejected(self):
  (self.assets/'boxlite-shim').chmod(0o755);(self.assets/'boxlite-shim').write_bytes(b'changed')
  with self.assertRaisesRegex(ValueError,'hash mismatch'):m.trusted_runtime(self.selected,'Linux')
 def test_extra_file_rejected(self):
  (self.assets/'extra').touch()
  with self.assertRaisesRegex(ValueError,'Unlisted'):m.trusted_runtime(self.selected,'Linux')
 def test_symlink_rejected(self):
  p=self.assets/'boxlite-shim';p.unlink();p.symlink_to(self.assets/'boxlite-guest')
  with self.assertRaises(OSError):m.trusted_runtime(self.selected,'Linux')
 def test_world_writable_rejected(self):
  (self.assets/'boxlite-shim').chmod(0o777)
  with self.assertRaisesRegex(ValueError,'Unsafe'):m.trusted_runtime(self.selected,'Linux')
 def test_path_traversal_rejected(self):
  self.files['../escape']='a'*64
  with self.assertRaises(ValueError):m.trusted_runtime(self.selected,'Linux')
 def test_incomplete_rejected(self):
  del self.files['bwrap']
  with self.assertRaisesRegex(ValueError,'Incomplete'):m.trusted_runtime(self.selected,'Linux')
 def test_directory_symlink_rejected(self):
  link=self.root/'link';link.symlink_to(self.assets);self.selected['runtime_assets']['directory']=str(link)
  with self.assertRaisesRegex(ValueError,'canonical'):m.trusted_runtime(self.selected,'Linux')
 def test_launcher_only_forwards_verified_selection(self):
  root=self.root/'node';b=root/'bin';b.mkdir(parents=True);(root/'node.json').write_text('{}');(root/'node.json').chmod(0o600)
  shutil.copy2(REPO/'scripts/serve.py',b/'serve.py');shutil.copy2(REPO/'examples/prime/node_paths.py',b/'node_paths.py')
  (b/'read-rest-key.py').write_text("print('a'*64)\n")
  for selected in [{},self.selected,{**self.selected,'guest_privileged_default':True}]:
   op={'rest_endpoint':'http://127.0.0.1:8090',**selected};p=root/'operator.json';p.write_text(json.dumps(op));p.chmod(0o600)
   with patch.dict(os.environ,{'BOXLITE_RUNTIME_DIR':'/untrusted/inherited'}),patch('os.execve') as execute,patch('sys.path',list(__import__('sys').path)):
    runpy.run_path(str(b/'serve.py'));env=execute.call_args.args[2]
    self.assertEqual(env.get('BOXLITE_RUNTIME_DIR'),str(self.assets) if selected else None)
    self.assertEqual(env['BOXLITE_SERVE_API_KEY'],'a'*64)
    self.assertEqual('--guest-privileged-default' in execute.call_args.args[1],selected.get('guest_privileged_default',False))
if __name__=='__main__':unittest.main()
