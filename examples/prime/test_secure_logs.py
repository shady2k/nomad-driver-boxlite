import test_support
import unittest,tempfile,io,os,json,sys
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent;sys.path.insert(0,str(HERE))
import secure_logs
class SecureLogs(unittest.TestCase):
 def test_raw_traceback_and_non_json_retained_secret_variants_redacted(self):
  secret='literal/with space+value';r=secure_logs.Redactor([secret])
  text='Traceback (most recent call last):\n  File "launcher.py", line 42, in child\nRuntimeError: missing native module\nAuthorization: Bearer fixture-bearer\nLLM_API_KEY="fixture-key"\nhttps://service/path?token=fixture-query&stage=clone\nhttps://user:pass@service/path\n'+secret+'\n'
  safe=r.text(text)
  for v in [secret,'fixture-bearer','fixture-key','fixture-query','user:pass']:self.assertNotIn(v,safe)
  for v in ['Traceback','launcher.py','line 42','missing native module','stage=clone']:self.assertIn(v,safe)
  with tempfile.TemporaryDirectory() as d:
   s=secure_logs.Spool(Path(d)/'logs',r,segment_bytes=250,max_bytes=4000)
   s.append('stderr',text);s.append('stdout','next raw line\n')
   files=sorted((Path(d)/'logs').glob('*.jsonl'));self.assertEqual(len(files),2)
   data=[json.loads(line) for f in files for line in f.read_text().splitlines()]
   self.assertEqual(data[0]['text'],safe);self.assertEqual(data[1]['text'],'next raw line\n')
   self.assertEqual(files[0].stat().st_mode&0o777,0o600)
 def test_both_secret_values_env_url_and_plain_traceback(self):
  api='fixture-llm/secret+with space';seed='a1'*32
  r=secure_logs.Redactor([api,seed])
  text='Traceback at bootstrap.py:91\nValueError: provider projection failed\nLLM_API_KEY="'+api+'"\nBOXLITE_DISPATCHER_SEED='+seed+'\nplain error embeds '+seed+'\nhttps://endpoint/path?api_key='+__import__('urllib.parse',fromlist=['quote']).quote(api,safe='')+'&token=fixture-query&phase=bootstrap\n'
  out=r.text(text)
  for value in [api,seed,'fixture-query',__import__('urllib.parse',fromlist=['quote']).quote(api,safe='')]:self.assertNotIn(value,out)
  for value in ['Traceback at bootstrap.py:91','ValueError: provider projection failed','plain error embeds','phase=bootstrap','BOXLITE_DISPATCHER_SEED']:self.assertIn(value,out)
 def test_rotation_never_deletes_old_segments_and_quota_explicit(self):
  with tempfile.TemporaryDirectory() as d:
   s=secure_logs.Spool(Path(d)/'logs',segment_bytes=10,max_bytes=260)
   s.append('stderr','first');s.append('stderr','second')
   before={f.name:f.read_bytes() for f in s.path.glob('*.jsonl')}
   with self.assertRaisesRegex(RuntimeError,'quota_exceeded'):s.append('stderr','x'*400)
   self.assertEqual(before,{f.name:f.read_bytes() for f in s.path.glob('*.jsonl')})
 def test_split_writes_on_one_line(self):
  out=io.StringIO();r=secure_logs.Redactor(['fixture-secret']);w=secure_logs.RedactedWriter(out,r)
  w.write('fixture-');w.write('secret\n');w.flush();self.assertEqual(out.getvalue(),'[REDACTED]\n')
if __name__=='__main__':unittest.main()
