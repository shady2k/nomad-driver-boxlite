import importlib.util,os,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('node_paths',Path(__file__).resolve().parents[1]/'examples/prime/node_paths.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Paths(unittest.TestCase):
 def test_defaults(self):
  self.assertEqual(m.default_root('Linux','/home/operator',0),Path('/var/lib/boxlite-nomad'))
  self.assertEqual(m.default_root('Linux','/home/operator',1000),Path('/home/operator/.local/state/boxlite-nomad'))
  self.assertEqual(m.default_root('Darwin','/Users/operator',501),Path('/Users/operator/Library/Application Support/BoxLiteNomad'))
 def test_release_and_symlink(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d).resolve();(r/'node.json').write_text('{}');(r/'node.json').chmod(0o600);f=r/'releases/v1/companion/module.py';f.parent.mkdir(parents=True);f.touch();(r/'current').symlink_to(r/'releases/v1');self.assertEqual(m.root(r/'current/companion/module.py'),r)
 def test_marker_at_module_root(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d).resolve();(r/'node.json').write_text('{}');(r/'node.json').chmod(0o600);self.assertEqual(m.root(r/'entry.py'),r)
 def test_unsafe_marker_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d).resolve();(r/'node.json').write_text('{}');(r/'node.json').chmod(0o644)
   with self.assertRaises(ValueError):m.root(r/'entry.py')
 def test_no_platform_substitution(self):
  with self.assertRaises(ValueError):m.default_root('Windows','/tmp',0)
if __name__=='__main__':unittest.main()
