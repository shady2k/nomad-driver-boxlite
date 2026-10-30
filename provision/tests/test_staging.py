"""Run real Ansible staging locally with synthetic credentials and inert artifacts.
Does not execute artifacts, activate services, or touch installed Mac state.
"""
import json,os,subprocess,tempfile,pathlib,hashlib,pwd,grp,unittest
P=pathlib.Path(__file__).resolve().parents[1]
class Stage(unittest.TestCase):
 def test_linux_and_darwin_staging(self):
  for system,remote in [('Linux',False),('Darwin',False),('Linux',True)]:
   with self.subTest(system=system,remote_artifacts=remote),tempfile.TemporaryDirectory(prefix='boxlite-role-test-') as td:
    d=pathlib.Path(td);private=d/'private';private.mkdir()
    for name in ['envconsul.hcl','legacy-bindings.json','models.json','session-policy.json']:(private/name).write_text('{}')
    artifact=d/'inert';artifact.write_text('inert fixture, not executable\n');sha=hashlib.sha256(artifact.read_bytes()).hexdigest()
    user=pwd.getpwuid(os.getuid());group=grp.getgrgid(os.getgid()).gr_name
    v={'boxlite_node_system':system,'boxlite_node_arch':'arm64','boxlite_service_user':user.pw_name,'boxlite_service_group':group,'boxlite_service_home':str(d/'home & fixtures'),'boxlite_node_root':str(d/'node & fixtures'),'boxlite_source_root':os.environ.get('BOXLITE_SOURCE_ROOT',str(P.parent)),'boxlite_private_assets':str(private),'boxlite_activation_config':{'enabled':True,'auth_mode':'openbao-hmac-v1','vault_addr':'https://bao.example.invalid','vault_token_file':str(d/'not-created-token')},'boxlite_operator_config':{'rest_endpoint':'http://127.0.0.1:9091','outbound_allow_all':True,'outbound_allow_net':[]},'boxlite_advertise_ip':'192.0.2.1','boxlite_source_ip':'192.0.2.2','boxlite_nomad_client_config':{'client':{'enabled':True,'servers':['192.0.2.3:4647'],'reserved':{'cpu':1000,'memory':2048}}},'boxlite_artifacts':{name:{'src':str(artifact),'sha256':sha,'version':'fixture','os':system.lower(),'arch':'arm64'} for name in ['nomad','boxlite','nomad-driver-boxlite','boxlite-stdio','envconsul','bao']}}
    # Role currently restricts routes to explicit RFC1918 operator addresses.
    v.update(boxlite_advertise_ip='10.0.0.1',boxlite_source_ip='10.0.0.2')
    if remote:
     for a in v['boxlite_artifacts'].values():a['remote_src']=True
    vf=d/'vars.json';vf.write_text(json.dumps(v));inventory=d/'inventory';inventory.write_text('boxlite_nodes ansible_connection=local ansible_become=false\n')
    cmd=['ansible-playbook','-i',str(inventory),str(P/'site.yml'),'-e','@'+str(vf)]
    env={**os.environ,'ANSIBLE_LOCAL_TEMP':str(d/'ansible-tmp')}
    for flags in [['--check'],[],['--check']]:
     r=subprocess.run(cmd+flags,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
     self.assertEqual(r.returncode,0,r.stdout[-7000:])
     if flags and (d/'node & fixtures/releases').exists():self.assertRegex(r.stdout,r'changed=0\s')
    self.assertFalse((d/'node & fixtures/current').exists(),'staging activated a release')
    services=list((d/'node & fixtures/releases').glob('*/services/*'))
    self.assertEqual(len(services),2)
    if system=='Darwin':
     import plistlib
     for f in services:self.assertTrue(plistlib.loads(f.read_bytes())['AbandonProcessGroup'])
if __name__=='__main__':unittest.main()
