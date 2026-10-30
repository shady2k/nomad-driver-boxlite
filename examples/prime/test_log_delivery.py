import unittest,tempfile,io,json
from pathlib import Path
from unittest.mock import Mock
import secure_logs as s
class Tests(unittest.TestCase):
 def test_epipe_retains_complete_redacted_text_and_records_missing_copy(self):
  with tempfile.TemporaryDirectory() as td:
   spool=s.Spool(Path(td)/'logs',s.Redactor(['SECRET']));target=Mock();target.write.side_effect=BrokenPipeError();state=set()
   text=s.capture_and_mirror(spool,target,'stderr',b'Traceback SECRET RuntimeError\n',state)
   self.assertNotIn('SECRET',text);raw=''.join(p.read_text() for p in spool.path.glob('*.jsonl'));self.assertIn('Traceback',raw);self.assertIn('RuntimeError',raw);self.assertIn('nomad_log_delivery_unavailable',raw);self.assertNotIn('SECRET',raw);self.assertIn('stderr',state)
 def test_capture_quota_remains_fatal(self):
  with tempfile.TemporaryDirectory() as td:
   spool=s.Spool(Path(td)/'logs',max_bytes=1)
   with self.assertRaises(RuntimeError):s.capture_and_mirror(spool,io.StringIO(),'stdout',b'line',set())
 def test_mirror_recovery_clears_delivery_state(self):
  with tempfile.TemporaryDirectory() as td:
   spool=s.Spool(Path(td)/'logs');state={'stdout'};target=io.StringIO();s.capture_and_mirror(spool,target,'stdout',b'line\n',state);self.assertFalse(state);self.assertEqual(target.getvalue(),'line\n')
if __name__=='__main__':unittest.main()
