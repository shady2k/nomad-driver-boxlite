import test_support
import json,os,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
import acp_bridge

class StartupReceipt(unittest.TestCase):
 def test_ready_receipt_is_bound_private_and_idempotent(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);directory=root/'driver-state/companions'/('a'*64);task=directory/'task';task.mkdir(parents=True,mode=0o700)
   aid=str(uuid.uuid4())
   with patch.object(acp_bridge,'ROOT',root),patch.dict(os.environ,{'NOMAD_TASK_DIR':str(task),'NOMAD_ALLOC_ID':aid}):
    acp_bridge.application_ready_receipt({'vm_id':'fixture-vm'})
    acp_bridge.application_ready_receipt({'vm_id':'fixture-vm'})
    p=directory/'application-ready.json';self.assertEqual(p.stat().st_mode&0o777,0o600)
    self.assertEqual(json.loads(p.read_text()),{'version':1,'allocation_id':aid,'vm_id':'fixture-vm'})
    with self.assertRaises(ValueError):acp_bridge.application_ready_receipt({'vm_id':'other-vm'})
 def test_wrong_companion_root_is_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   with patch.object(acp_bridge,'ROOT',Path(d)),patch.dict(os.environ,{'NOMAD_TASK_DIR':d+'/wrong/task','NOMAD_ALLOC_ID':str(uuid.uuid4())}):
    with self.assertRaises(ValueError):acp_bridge.application_ready_receipt({'vm_id':'fixture-vm'})
