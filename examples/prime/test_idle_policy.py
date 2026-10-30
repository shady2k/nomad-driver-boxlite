import unittest,tempfile,json,uuid,time
from pathlib import Path
from unittest.mock import patch
import idle_policy as i
class Tests(unittest.TestCase):
 def fixture(self):return 'worker-'+str(uuid.uuid4()),{'session_id':str(uuid.uuid4()),'state':'ready','requests':[]}
 def test_default_no_grace_no_auto_close(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)):
   j,s=self.fixture();o=i.decorate(j,s,{'state':'ready-exported'},now=100);self.assertIsNone(o['idle_grace_seconds']);self.assertEqual(o['idle_policy_state'],'grace_unconfigured');self.assertFalse(o['auto_close_eligible'])
 def test_busy_multiday_not_idle(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)):
   j,s=self.fixture();s.update(state='busy',active_request_id='request');o=i.decorate(j,s,{'state':'busy'},now=10000000);self.assertIsNone(o['idle_elapsed_seconds']);self.assertEqual(o['idle_policy_state'],'not_idle')
 def test_configured_grace_observed_conservatively(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)):
   p=Path(t)/'.config/nomad-boxlite-prime/lifecycle.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'auto_close_enabled':False,'idle_grace_seconds':10}));p.chmod(0o600)
   j,s=self.fixture();self.assertEqual(i.decorate(j,s,{'state':'ready-exported'},now=100)['idle_policy_state'],'within_grace');o=i.decorate(j,s,{'state':'ready-exported'},now=111);self.assertEqual(o['idle_policy_state'],'grace_elapsed');self.assertFalse(o['auto_close_enabled'])
   s['requests']=[{'request_id':'new','state':'completed'}];self.assertEqual(i.decorate(j,s,{'state':'ready-exported'},now=112)['idle_elapsed_seconds'],0)
 def test_unexported_and_unknown_never_eligible(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)):
   j,s=self.fixture()
   for state in ['unknown','ready-unexported','awaiting-input']:
    o=i.decorate(j,s,{'state':state},now=100);self.assertFalse(o['auto_close_eligible']);self.assertNotEqual(o['idle_policy_state'],'grace_elapsed')
 def test_hold_release_and_expiry(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)),patch.object(i.time,'time',return_value=100):
   j,s=self.fixture();i.hold(j,s['session_id'],20);self.assertTrue(i.decorate(j,s,{'state':'ready-exported'},now=101)['hold_active']);self.assertFalse(i.decorate(j,s,{'state':'ready-exported'},now=121)['hold_active']);i.hold(j,s['session_id'],release=True);self.assertFalse(i.decorate(j,s,{'state':'ready-exported'},now=102)['hold_active'])
 def test_invalid_config_fails_closed(self):
  with tempfile.TemporaryDirectory() as t,patch.object(i.Path,'home',return_value=Path(t)):
   p=Path(t)/'.config/nomad-boxlite-prime/lifecycle.json';p.parent.mkdir(parents=True);p.write_text('{"auto_close_enabled":true,"idle_grace_seconds":0}');p.chmod(0o600);j,s=self.fixture();o=i.decorate(j,s,{'state':'ready-exported'});self.assertEqual(o['idle_policy_state'],'unknown');self.assertFalse(o['auto_close_enabled'])
