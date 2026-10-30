"""Infrastructure-owned generic request policy. No project/profile selection."""
import copy, hashlib, hmac, json, re, uuid
NAMESPACE='boxlite-prime'
POLICY_PATH='nomad/jobs'
POLICY_ITEM='boxlite_node_budgets'
BUDGET_FIELDS={'max_vms','max_guest_cpus','max_reservation_mib','memory_overhead_mib','cpu_mhz_per_vcpu','default_disk_gib','max_disk_gib'}
def parse_catalog(variable):
 try:
  data=json.loads(variable['Items'][POLICY_ITEM])
  if set(data)!={'version','nodes'} or data['version']!=1 or not isinstance(data['nodes'],dict) or not data['nodes']:raise ValueError()
  for node,b in data['nodes'].items():
   str(uuid.UUID(node))
   if set(b)!=BUDGET_FIELDS or any(type(v) is not int or v<=0 for v in b.values()) or b['default_disk_gib']>b['max_disk_gib']:raise ValueError()
  return {'variable':NAMESPACE+':'+POLICY_PATH,'modify_index':variable['ModifyIndex'],'nodes':data['nodes'],'scope':'Per-node admission, not a global scheduler quota'}
 except (ValueError,TypeError,KeyError):raise ValueError('Missing or invalid operator BoxLite budget Variable '+NAMESPACE+':'+POLICY_PATH)
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
def validate(value):
 if not isinstance(value,dict) or set(value)-{'image','cpus','memory_mib','disk_gib','request_id','session_id','repo','files','commands','prompt','context','node_id','model','architecture'}:raise ValueError('Unknown universal request fields')
 d=copy.deepcopy(value)
 if 'model' in d:
  from model_policy import model_id
  model_id(d['model'])
 for k in ['request_id','session_id']:d[k]=str(uuid.UUID(d[k]))
 if not isinstance(d.get('image'),str) or not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}',d['image']):raise ValueError('Explicit pinned image required')
 for k in ['cpus','memory_mib','disk_gib']:
  if type(d.get(k)) is not int or d[k]<=0:raise ValueError('Explicit positive resource required: '+k)
 if 'node_id' in d:d['node_id']=str(uuid.UUID(d['node_id']))
 if 'architecture' in d and d['architecture'] not in ('amd64','arm64'):raise ValueError('Image architecture must be amd64 or arm64')
 if 'node_id' not in d and 'architecture' not in d:raise ValueError('Image architecture required for Nomad placement; no default NodeID')
 # Reuse legacy initialization safety checks, never its resource/profile choice.
 import contract
 init={k:v for k,v in d.items() if k not in ['image','cpus','memory_mib','disk_gib','node_id','model','architecture']}
 checked=contract.validate_initialization(init)
 for k in ['repo','files','commands','prompt','context']:
  if k in checked:d[k]=checked[k]
 if len(canonical(d))>15500:raise ValueError('Initialization payload too large; use file transfer')
 return d

def identity(value):return hashlib.sha256(b'boxlite-request-v1\0'+canonical(validate(value))).hexdigest()
def parent(value):return 'worker-'+str(uuid.UUID(value['request_id']))
def is_parent(name):return bool(re.fullmatch(r'worker-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',name or ''))
def derive(seed,session_id,spec_id):
 if len(seed)!=32 or not re.fullmatch('[a-f0-9]{64}',spec_id):raise ValueError('Invalid request identity')
 return hmac.new(seed,b'boxlite-session-v3\0'+NAMESPACE.encode()+b'\0'+spec_id.encode()+b'\0'+str(uuid.UUID(session_id)).encode(),hashlib.sha256).hexdigest()
def project_slug(value):
 if not isinstance(value,str) or not 1<=len(value)<=64 or not re.fullmatch(r'[A-Za-z0-9 _-]+',value):raise ValueError('Project must be an explicit 1..64 character label, not a path or URL')
 slug=re.sub(r'[- _]+','-',value.lower()).strip('-')
 if not slug:raise ValueError('Project label is empty')
 return slug

def display_name(value,project=None):
 job=parent(value)
 return job if project is None else project_slug(project)+'-'+job

def valid_display_name(name,value):
 base=parent(value)
 return name==base or (isinstance(name,str) and name.endswith('-'+base) and bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name[:-len(base)-1])) and len(name[:-len(base)-1])<=64)

def inventory_budget_details(catalog):
 inventory=catalog.get('placement_inventory',{})
 rows=inventory.get('nodes',[])
 details={n['id']:n for n in rows if isinstance(n,dict) and isinstance(n.get('id'),str)}
 known=inventory.get('complete') is True and set(catalog['nodes'])<=set(details)
 known=known and all(details[n].get('host_architecture') in ('amd64','arm64') for n in catalog['nodes'])
 return details if known else None

def applicable_budgets(catalog,node_id=None,architecture=None):
 # Static accounting from operator budgets, grouped by real Nomad attributes.
 # Never choose a node or consult free capacity/health/eligibility here.
 if node_id is not None:
  if node_id not in catalog['nodes']:raise ValueError('Node has no operator budget in '+catalog['variable'])
  return [catalog['nodes'][node_id]]
 details=inventory_budget_details(catalog)
 if architecture is not None and details is not None:
  budgets=[b for n,b in catalog['nodes'].items() if details[n].get('host_architecture')==architecture and details[n].get('datacenter')=='dc1' and details[n].get('node_pool')=='default' and details[n].get('boxlite_api')=='v1']
  if not budgets:raise ValueError('No compatible Nomad node with operator budget for image architecture')
  return budgets
 # Existing identities may lack node:read. Preserve the conservative contract;
 # callers surface incomplete inventory instead of inventing a platform map.
 return list(catalog['nodes'].values())

def request_policy(catalog,node_id=None,architecture=None):
 budgets=applicable_budgets(catalog,node_id,architecture)
 return {k:max(b[k] for b in budgets) for k in BUDGET_FIELDS}

def build_job(value,catalog,project=None,required_guest_features=()):
 import contract
 d=validate(value);name=parent(d);sid=identity(d);node_id=d.get('node_id')
 policy=request_policy(catalog,node_id,d.get('architecture'))
 constraints=[{'LTarget':'${attr.driver.boxlite.api}','Operand':'=','RTarget':'v1'}]
 for feature in required_guest_features:
  if feature not in ('docker','tun','netem','writable-cgroups'):raise ValueError('Unknown required guest capability')
  constraints.append({'LTarget':'${attr.driver.boxlite.guest.'+feature+'}','Operand':'=','RTarget':'true'})
 if node_id is not None:constraints.insert(0,{'LTarget':'${node.unique.id}','Operand':'=','RTarget':node_id})
 if 'architecture' in d:constraints.append({'LTarget':'${attr.cpu.arch}','Operand':'=','RTarget':d['architecture']})
 network=contract._pm.CONFIG['ports'].get('host_network')
 if not isinstance(network,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',network):raise ValueError('Explicit operator host network label required')
 if not any(d['cpus']<=b['max_guest_cpus'] and d['memory_mib']+policy['memory_overhead_mib']<=b['max_reservation_mib'] and d['disk_gib']<=b['max_disk_gib'] for b in applicable_budgets(catalog,node_id,d.get('architecture'))):raise ValueError('Requested resources exceed every applicable operator node budget')
 vm={'Name':'vm','Driver':'boxlite','Config':{'image':d['image'],'guest_cpus':d['cpus'],'memory_mib':d['memory_mib'],'disk_gib':d['disk_gib'],'stdio_mode':'external','restricted_network':True,'single_allocation':True,'logical_id':d['session_id'],'worker_request':canonical(d).decode()},'Resources':{'CPU':d['cpus']*policy['cpu_mhz_per_vcpu'],'MemoryMB':d['memory_mib']+policy['memory_overhead_mib']},'KillTimeout':30000000000,'Identity':{'Env':True,'ChangeMode':'noop'}}
 return {'ID':name,'Name':display_name(d,project),'Namespace':NAMESPACE,'Type':'batch','Datacenters':['dc1'],'Meta':{'spec_id':sid,'request_id':d['request_id'],'session_id':d['session_id']},'Constraints':constraints,'TaskGroups':[{'Name':'worker','Count':1,'EphemeralDisk':{'SizeMB':d['disk_gib']*1024+1024,'Sticky':False,'Migrate':False},'Networks':[{'Mode':'host','DynamicPorts':[{'Label':'session','HostNetwork':network}]}],'Disconnect':{'LostAfter':7200000000000,'Replace':False,'Reconcile':'keep_original'},'RestartPolicy':{'Attempts':0,'Mode':'fail','Interval':1800000000000,'Delay':15000000000},'ReschedulePolicy':{'Attempts':0,'Unlimited':False},'Tasks':[vm]}]}

def verify_job(value,job,catalog):
 # Compare only server-stable immutable task fields; reject caller-supplied tasks/mounts/env/templates.
 expected=build_job(value,catalog)
 if not valid_display_name(job.get('Name'),value):raise ValueError('Untrusted job display name')
 for k in ['ID','Namespace','Type','Constraints','ParameterizedJob','Meta']:
  if job.get(k)!=expected.get(k):raise ValueError('Untrusted job field '+k)
 if job.get('TaskGroups')!=expected['TaskGroups']:raise ValueError('Untrusted task specification')
 return identity(value)
