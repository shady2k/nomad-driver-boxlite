import unittest,sys,tempfile,io,hashlib,json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).parent));import multipart as m,exchange as e
class Tests(unittest.TestCase):
 def plan(self,remote=None):return {'upload_id':'synthetic-upload','part_bytes':3,'parts':[{'number':i+1,'size':3,'put_url':'https://example.test/part'+str(i+1),'remote':(remote or {}).get(i+1)} for i in range(2)]}
 def test_interruption_resumes_exact_checkpoint_part(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'state.json';sent=[]
   def fail(url,raw):
    sent.append(url)
    if url.endswith('2'):raise OSError('synthetic interruption')
    return 'ETAG1'
   with self.assertRaises(OSError):m.send_parts(io.BytesIO(b'abcdef'),self.plan(),p,fail)
   self.assertEqual(list(e.protected(p)['parts']),['1']);sent=[]
   r=m.send_parts(io.BytesIO(b'abcdef'),self.plan({1:{'etag':'ETAG1','size':3}}),p,lambda u,r:(sent.append(u) or 'ETAG2'))
   self.assertEqual(r['resumed_parts'],1);self.assertEqual(sent,['https://example.test/part2']);self.assertNotIn('https://',p.read_text())
 def test_wrong_remote_etag_reuploads(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'s';m.send_parts(io.BytesIO(b'abcdef'),self.plan(),p,lambda *a:'old');sent=[];r=m.send_parts(io.BytesIO(b'abcdef'),self.plan({1:{'etag':'other','size':3},2:{'etag':'old','size':3}}),p,lambda u,r:(sent.append(u) or 'new'));self.assertEqual(r['resumed_parts'],1);self.assertEqual(len(sent),1)
 def test_changed_source_part_reuploads(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'s';m.send_parts(io.BytesIO(b'abcdef'),self.plan(),p,lambda *a:'old');sent=[];r=m.send_parts(io.BytesIO(b'xxxdef'),self.plan({i:{'etag':'old','size':3} for i in (1,2)}),p,lambda u,r:(sent.append(u) or 'new'));self.assertEqual(r['resumed_parts'],1);self.assertEqual(len(sent),1)
 def test_different_upload_checkpoint_refused(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'s';m.atomic(p,{'upload_id':'other','parts':{}})
   with self.assertRaises(e.ExchangeError):m.send_parts(io.BytesIO(b'abcdef'),self.plan(),p,lambda *a:'e')
 def test_init_uncertain_no_duplicate_start(self):
  with tempfile.TemporaryDirectory() as t:
   manager=m.Manager(SimpleNamespace(url=lambda *a,**k:'https://example.test/part'),t)
   with patch.object(m,'begin',side_effect=e.ExchangeError('network')) as start:
    with self.assertRaises(e.ExchangeError):manager.plan('key',9,'a'*64)
    with self.assertRaisesRegex(e.ExchangeError,'outcome_unknown'):manager.plan('key',9,'a'*64)
    self.assertEqual(start.call_count,1)
 def test_cross_key_checkpoints_distinct(self):
  with tempfile.TemporaryDirectory() as t:
   manager=m.Manager(SimpleNamespace(url=lambda *a,**k:'https://example.test/part'),t)
   with patch.object(m,'begin',return_value='id'),patch.object(m,'list_parts',return_value={}):manager.plan('a',9,'a'*64);manager.plan('b',9,'a'*64)
   self.assertEqual(len(list(Path(t).glob('*.json'))),2)
 def test_missing_parts_cannot_complete(self):
  with tempfile.TemporaryDirectory() as t:
   manager=m.Manager(SimpleNamespace(url=lambda *a,**k:'https://example.test/part'),t)
   with patch.object(m,'begin',return_value='id'),patch.object(m,'list_parts',return_value={}):manager.plan('a',m.PART+1,'a'*64)
   with patch.object(m,'list_parts',return_value={}),patch.object(m,'complete') as complete:
    with self.assertRaises(e.ExchangeError):manager.finish('a',m.PART+1,'a'*64)
    complete.assert_not_called()
if __name__=='__main__':unittest.main()
