import unittest,importlib.util,hashlib
from pathlib import Path
s=importlib.util.spec_from_file_location('cleanup_guard',Path(__file__).resolve().parents[2]/'scripts/cleanup-stopped.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Tests(unittest.TestCase):
 def record(self,destroyed):return {'VMID':'vm','Key':hashlib.sha256(b'alloc\0task').hexdigest(),'AllocID':'alloc','TaskName':'task','Destroyed':destroyed}
 def test_released_only(self):self.assertEqual(len(m.require_released([self.record(True)],'vm')),1)
 def test_live_unreleased_unknown_denied(self):
  for records in [[],[self.record(False)],[{'VMID':'other'}]]:
   with self.assertRaises(ValueError):m.require_released(records,'vm')
 def test_tampered_identity_denied(self):
  r=self.record(True);r['AllocID']='other'
  with self.assertRaises(ValueError):m.require_released([r],'vm')
