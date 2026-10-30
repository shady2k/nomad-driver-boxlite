import unittest,tempfile,json,uuid,sys
from pathlib import Path
from unittest.mock import patch
import durable_export as d,exchange
JOB='worker-'+str(uuid.uuid4());SID=str(uuid.uuid4());RID=str(uuid.uuid4());SHA='a'*64
class Store:
 def manifest(self,*a):return {'scope':'declared-terminal-results','files':[{'path':'.nomad-results/'+RID+'.json','size':3,'sha256':SHA}]}
 def verify(self,*a):pass
 def stat(self,*a):return {'etag':'verified-etag','bytes':3}
class Tests(unittest.TestCase):
 def test_ack_then_ready_exported_and_unavailable_not_exported(self):
  with tempfile.TemporaryDirectory() as t,patch.object(Path,'home',return_value=Path(t)):
   root=d.base();root.mkdir(mode=0o700,parents=True);cfg=Path(t)/'.config/nomad-boxlite-prime/exchange-reader.json';cfg.parent.mkdir(parents=True);cfg.write_text('{}');cfg.chmod(0o600)
   info={'session_id':SID,'state':'ready','requests':[{'request_id':RID,'state':'completed'}]};receipt={'job':JOB,'session_id':SID,'request_id':RID,'scope':'declared-terminal-results','manifest_sha256':SHA}
   with patch.object(exchange,'config',return_value={}),patch.object(exchange,'Store',return_value=Store()):
    ack=d.s3_ack(JOB,info,receipt);self.assertEqual(ack['storage_kind'],'s3');state=d.classify(JOB,info);self.assertEqual(state['state'],'ready-exported');self.assertEqual(state['storage_kind'],'s3');self.assertFalse(state['auto_close_enabled'])
   with patch.object(exchange,'config',return_value={}),patch.object(exchange,'Store',side_effect=OSError):self.assertNotEqual(d.classify(JOB,info)['state'],'ready-exported')
 def test_snapshot_not_terminal_ack(self):
  with self.assertRaises(ValueError):d.s3_ack(JOB,{'session_id':SID,'state':'ready'},{'job':JOB,'session_id':SID,'request_id':RID,'scope':'selected-snapshot'})
 def test_no_s3_receipt_never_reads_config_or_key(self):
  with tempfile.TemporaryDirectory() as t,patch.object(Path,'home',return_value=Path(t)),patch.object(exchange,'Store',side_effect=AssertionError('must not read S3')):
   d.base().mkdir(mode=0o700,parents=True);state=d.classify(JOB,{'session_id':SID,'state':'ready','requests':[{'request_id':RID,'state':'completed'}]});self.assertNotEqual(state['state'],'ready-exported')
