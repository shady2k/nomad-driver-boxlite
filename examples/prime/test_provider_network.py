import test_support
import json,socket,tempfile,unittest,importlib.util
from pathlib import Path
from unittest.mock import patch
import acp_bridge
spec=importlib.util.spec_from_file_location('provider_guest',Path(__file__).parent/'guest/guest.py');guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)
class ProviderNetwork(unittest.TestCase):
 def resolve(self,rules,address='10.20.30.40'):
  def read(p):
   if p.name=='session-policy.json':return {'provider':'fixture'}
   if p.name=='models.json':return {'providers':{'fixture':{'baseUrl':'https://gateway.example.test/v1'}}}
   return {'outbound_allow_net':rules,'outbound_allow_all':False}
  with patch.object(acp_bridge.gateway,'protected_json',side_effect=read),patch.object(acp_bridge.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]):return acp_bridge.provider_host_aliases()
 def test_hostname_alone_cannot_grant_private_destination(self):
  with self.assertRaisesRegex(RuntimeError,'explicit_operator_allowlist'):self.resolve(['gateway.example.test'])
 def test_exact_operator_ip_enables_only_selected_alias(self):
  self.assertEqual(self.resolve(['gateway.example.test','10.20.30.40/32']),{'gateway.example.test':['10.20.30.40']})
 def test_other_ip_is_not_implicit_permission(self):
  with self.assertRaises(RuntimeError):self.resolve(['gateway.example.test','10.20.30.41/32'])
 def test_public_provider_uses_normal_dns(self):
  self.assertEqual(self.resolve(['gateway.example.test'],'8.8.8.8'),{})
 def test_loopback_never_re_admitted_by_helper(self):
  with self.assertRaises(RuntimeError):self.resolve(['gateway.example.test','127.0.0.1/32'],'127.0.0.1')
 def test_hosts_preserved_and_idempotent(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'hosts';p.write_text('127.0.0.1 localhost\n10.0.0.1 unrelated.example.test\n');aliases={'gateway.example.test':['10.20.30.40']}
   guest.apply_provider_hosts(aliases,p);first=p.read_text();guest.apply_provider_hosts(aliases,p);self.assertEqual(first,p.read_text());self.assertIn('unrelated.example.test',first);self.assertEqual(first.count('gateway.example.test'),1)
