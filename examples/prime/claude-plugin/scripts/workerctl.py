#!/usr/bin/env python3
"""Persistent guest client. Dispatch once; follow-ups use the same endpoint."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import time
import socket
import http.client
import universal
import observability
import durable_export

spec=importlib.util.spec_from_file_location('worker_contract',Path(__file__).with_name('contract.py'))
contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)
spec=importlib.util.spec_from_file_location('dispatcher_auth',Path(__file__).with_name('dispatcher_auth.py'))
auth=importlib.util.module_from_spec(spec);spec.loader.exec_module(auth)
NAMESPACE,PARENT,PROFILE=contract.NAMESPACE,contract.PARENT,contract.PROFILE
child_job,validate=contract.child_job,contract.validate


def configured_nomad_origin():
    config=Path.home()/'.config/nomad-boxlite-prime/operator.json'
    origin=auth.protected_json(config).get('nomad_address','http://127.0.0.1:4646') if config.exists() else 'http://127.0.0.1:4646'
    if not isinstance(origin,str):raise ValueError('Invalid operator Nomad API origin')
    u=urllib.parse.urlsplit(origin)
    if u.username or u.password or u.query or u.fragment or u.path not in ('','/') or not u.hostname or (u.scheme!='https' and not (u.scheme=='http' and u.hostname=='127.0.0.1')):raise ValueError('Operator Nomad API requires HTTPS origin or loopback HTTP')
    return origin.rstrip('/')

class Nomad:
    def __init__(self):
        self.origin=configured_nomad_origin()
        token_file = Path.home()/'.config/nomad-boxlite-prime/token'
        st = token_file.stat()  # Missing activation fails before any HTTP request.
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or token_file.is_symlink():
            raise ValueError('Expected own protected Nomad token file (0600)')
        self.token = token_file.read_text().strip()
        if not self.token:
            raise ValueError('Nomad activation token is empty')

    def call(self, path, method='GET', body=None, text=False):
        sep = '&' if '?' in path else '?'
        request = urllib.request.Request(self.origin+path+sep+'namespace='+NAMESPACE,
            method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'X-Nomad-Token':self.token,'Content-Type':'application/json'})
        deadline=getattr(self,'wait_deadline',None)
        timeout=min(30,max(.001,deadline-time.monotonic())) if deadline is not None else 30
        if deadline is not None and deadline<=time.monotonic():raise TimeoutError('wait_deadline')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024*1024)
        return raw.decode(errors='replace') if text else json.loads(raw)


def resolve_platform_image(image,architecture):
    """Resolve an immutable OCI index before dispatch; never select a Nomad node."""
    import re,hashlib
    match=re.fullmatch(r'([^/]+)/([A-Za-z0-9._/-]+)@(sha256:[a-f0-9]{64})',image)
    if not match or architecture not in ('amd64','arm64'):raise ValueError('Immutable image digest and explicit target architecture required')
    host,repo,digest=match.groups()
    if '..' in repo.split('/'):raise ValueError('Invalid image repository')
    origin=configured_registry_origin('https://'+host)
    def read(kind,sha):
        req=urllib.request.Request(origin+'/v2/'+urllib.parse.quote(repo,safe='/')+'/'+kind+'/'+sha,headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json','Accept-Encoding':'identity'})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:raw=response.read(1048577)
        except (urllib.error.URLError,TimeoutError) as e:raise ValueError('Image platform metadata unavailable; no create; error_type='+type(e).__name__) from None
        if len(raw)>1048576 or 'sha256:'+hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Image metadata digest mismatch; no create')
        return json.loads(raw)
    manifest=read('manifests',digest)
    if 'manifests' in manifest:
        descriptors=manifest['manifests']
        if not isinstance(descriptors,list) or len(descriptors)>100:raise ValueError('Image platform descriptor limit')
        matches=[d for d in descriptors if d.get('platform',{}).get('os')=='linux' and d.get('platform',{}).get('architecture')==architecture]
        if len(matches)!=1:raise ValueError('Image index must declare exactly one matching Linux platform; no create')
        selected=matches[0].get('digest','')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',selected):raise ValueError('Invalid platform digest')
        # Validate the selected child's actual config, not just the descriptor.
        manifest=read('manifests',selected)
    else:selected=digest
    config_digest=manifest.get('config',{}).get('digest','')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',config_digest):raise ValueError('Missing image platform config')
    config=read('blobs',config_digest)
    if config.get('os')!='linux' or config.get('architecture')!=architecture:raise ValueError('Image platform config mismatch; no create')
    required=['docker','tun','netem','writable-cgroups'] if (config.get('config',{}).get('Labels') or {}).get('io.boxlite.guest.docker-default')=='1' else []
    return {'required_guest_features':required,'requested_image':image,'resolved_image':host+'/'+repo+'@'+selected,'architecture':architecture,'index_digest':digest if selected!=digest else None,'platform_digest':selected,'source':'verified OCI manifest/config digests','node_selection':'Nomad constraints; no default NodeID'}


def worker_compatibility(platforms,labels,architecture):
 arch={'aarch64':'arm64','x86_64':'amd64'}.get(architecture,architecture)
 if arch not in (None,'arm64','amd64'):raise ValueError('Unsupported target architecture')
 candidates=[p for p in platforms if isinstance(p,dict) and p.get('os')=='linux' and (not arch or p.get('architecture')==arch)]
 if platforms and not candidates:return {'state':'incompatible','reason':'platform_mismatch'}
 contract=labels.get('io.boxlite.worker-contract')
 if contract is not None and contract!='2':return {'state':'incompatible','reason':'unsupported_worker_contract'}
 if not arch:return {'state':'unknown','reason':'target_architecture_required'}
 if not candidates:return {'state':'unknown','reason':'platform_metadata_missing'}
 if contract is None:return {'state':'unknown','reason':'worker_contract_not_declared'}
 return {'state':'declared_compatible','reason':'linux_target_and_worker_contract_2','prime_version':labels.get('io.boxlite.prime-version'),'runtime_verified':False}

def configured_registry_origin(explicit=None):
    origin=explicit
    if origin is None:
        config=Path.home()/'.config/nomad-boxlite-prime/operator.json'
        origin=auth.protected_json(config).get('registry_origin') if config.exists() else None
    if not isinstance(origin,str) or not origin:
        raise ValueError('registry_not_configured; operator.json registry_origin or explicit --registry HTTPS origin required')
    parsed=urllib.parse.urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Registry must be a credential-free HTTPS origin')
    return origin.rstrip('/')

def declared_tool_metadata(labels):
    # Only explicit version labels. Never infer installed tools from image names,
    # descriptions, project labels, build history or arbitrary environment values.
    import re
    tools={}
    for key,value in labels.items():
        match=re.fullmatch(r'io\.boxlite\.tool\.([a-z0-9][a-z0-9+_.-]{0,63})\.version',key)
        if match and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+~-]{0,127}',value):tools[match.group(1)]=value
    return {'state':'declared' if tools else 'unknown','tools':tools,'runtime_verified':False,'reason':'explicit_version_labels' if tools else 'no_tool_version_labels; do not infer toolchain from image name'}


def registry_images(origin, repository=None, workers_only=False, architecture=None):
    parsed=urllib.parse.urlsplit(origin)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Registry must be a credential-free HTTPS origin')
    def read(path):
        req=urllib.request.Request(origin.rstrip('/')+'/v2/'+path,headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                raw=response.read(1048577)
                if len(raw)>1048576:raise ValueError('Registry response size exceeded')
                return json.loads(raw),response.headers.get('Docker-Content-Digest')
        except urllib.error.HTTPError as e:
            raise ValueError(('registry_denied' if e.code in (401,403) else 'registry_not_found' if e.code==404 else 'registry_unavailable_http_'+str(e.code))+'; operation=GET /v2/'+path) from None
        except (urllib.error.URLError,TimeoutError) as e:
            raise ValueError('registry_unreachable; operation=GET /v2/'+path+'; error_type='+type(e).__name__+'; not proof an image is absent') from None
    names=[repository] if repository else read('_catalog?n=100')[0].get('repositories',[])
    rows=[];warnings=[]
    for name in names:
        if not isinstance(name,str) or any(x in name for x in ['..','?','#',':']):raise ValueError('Invalid repository')
        try:
            for tag in read(urllib.parse.quote(name,safe='/')+'/tags/list?n=100')[0].get('tags') or []:
                manifest,digest=read(urllib.parse.quote(name,safe='/')+'/manifests/'+urllib.parse.quote(tag,safe=''))
                platforms=[x.get('platform') for x in manifest.get('manifests',[])]
                declared_labels={}
                resolved_digest=digest if not manifest.get('manifests') else None
                if manifest.get('manifests') and architecture:
                    target={'aarch64':'arm64','x86_64':'amd64'}.get(architecture,architecture)
                    descriptors=[x for x in manifest['manifests'] if x.get('platform',{}).get('os')=='linux' and x.get('platform',{}).get('architecture')==target]
                    if len(descriptors)==1:resolved_digest=descriptors[0].get('digest')
                    if len(descriptors)>20:raise ValueError('Platform descriptor limit exceeded')
                    declarations=[]
                    for descriptor in descriptors:
                        sub,_=read(urllib.parse.quote(name,safe='/')+'/manifests/'+urllib.parse.quote(descriptor['digest'],safe=':'))
                        cd=sub.get('config',{}).get('digest')
                        if not cd:continue
                        cfg,_=read(urllib.parse.quote(name,safe='/')+'/blobs/'+urllib.parse.quote(cd,safe=':'))
                        declarations.append({k:v[:1024] for k,v in (cfg.get('config',{}).get('Labels') or {}).items() if (k in ('io.boxlite.worker-contract','io.boxlite.prime-version','io.boxlite.acpx-version','io.boxlite.kernel-defaults','io.boxlite.guest.docker-default') or bool(declared_tool_metadata({k:v})['tools'])) and isinstance(v,str)})
                    if descriptors and len(declarations)==len(descriptors) and all(x.get('io.boxlite.worker-contract')==declarations[0].get('io.boxlite.worker-contract') for x in declarations):declared_labels=declarations[0]
                config_digest=manifest.get('config',{}).get('digest')
                if config_digest and not platforms:
                    cfg,_=read(urllib.parse.quote(name,safe='/')+'/blobs/'+urllib.parse.quote(config_digest,safe=':'))
                    platforms=[{k:cfg.get(k) for k in ('os','architecture','variant') if cfg.get(k)}]
                    declared_labels={k:v[:1024] for k,v in (cfg.get('config',{}).get('Labels') or {}).items() if (k in ('org.opencontainers.image.title','org.opencontainers.image.description','org.opencontainers.image.version','org.opencontainers.image.revision','io.boxlite.worker-contract','io.boxlite.prime-version','io.boxlite.acpx-version','io.boxlite.kernel-defaults','io.boxlite.guest.docker-default') or bool(declared_tool_metadata({k:v})['tools'])) and isinstance(v,str)}
                rows.append({'repository':name,'tag':tag,'digest':digest,'image':origin.split('://',1)[1].rstrip('/')+'/'+name+'@'+digest if digest else None,'resolved_digest':resolved_digest,'resolved_image':origin.split('://',1)[1].rstrip('/')+'/'+name+'@'+resolved_digest if resolved_digest else None,'platforms':platforms,'declared_labels':declared_labels,'config_digest':manifest.get('config',{}).get('digest'),'worker_compatibility':worker_compatibility(platforms,declared_labels,architecture),'tool_metadata':declared_tool_metadata(declared_labels)})
        except ValueError as e:
            if repository:raise
            warnings.append({'repository':name,'error':str(e),'not_proof_of_absence':True})
    unknown=[x for x in rows if x['worker_compatibility']['state']=='unknown'];incompatible=[x for x in rows if x['worker_compatibility']['state']=='incompatible']
    selected=[x for x in rows if x['worker_compatibility']['state']=='declared_compatible'] if workers_only else rows
    return {'registry':origin.rstrip('/'),'compatible_worker_count':sum(x['worker_compatibility']['state']=='declared_compatible' for x in rows),'images':selected,'unknown_images':unknown if workers_only else [],'incompatible_images':incompatible if workers_only else [],'target_architecture':architecture,'empty':not rows and not warnings,'partial':bool(warnings),'warnings':warnings,'scope':'First 100 repositories/tags; supply --repository to narrow. No credentials added; no project mapping.'}

def diagnostic_path(job):
    child_job(job)
    return Path.home()/'.local/state/nomad-boxlite-prime/diagnostics'/(__import__('hashlib').sha256(job.encode()).hexdigest()+'.json')

def saved_diagnostics(job):
    path=diagnostic_path(job)
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):raise ValueError('Unsafe saved diagnostics')
            raw=f.read(65537)
        if len(raw)>65536:raise ValueError('Saved diagnostics size exceeded')
        data=json.loads(raw)
        if data.get('job')!=job:raise ValueError('Saved diagnostics identity mismatch')
        return {**data,'source':'coordinator_saved_summary','raw_logs_restored':False}
    except FileNotFoundError:return None

def job_lifecycle(api,job):
    try:d=api.call('/v1/job/'+urllib.parse.quote(child_job(job),safe=''))
    except urllib.error.HTTPError as e:
        if e.code==404:return {'available':False,'cause':'job_history_missing'}
        raise
    if not isinstance(d,dict):return {'available':False,'cause':'unexpected_job_response'}
    return {'available':True,'display_name':d.get('Name',d.get('ID')),'stop':d.get('Stop'),'status':d.get('Status'),'version':d.get('Version'),'modify_index':d.get('ModifyIndex'),
            'meaning':'Job stop/status is current intent; allocation DesiredStatus is not proof a VM is alive or should remain running'}

def terminal_diagnostics(api,job):
    try:rows=allocations(api,job)
    except urllib.error.HTTPError as e:
        if e.code!=404:raise
        return saved_diagnostics(job) or {'job':job,'phase':'diagnostics_unavailable','cause':'server_history_missing_and_no_saved_summary','raw_logs_restored':False}
    summaries=[]
    for a in rows:
        for state in (a.get('TaskStates') or {}).values():
            for event in state.get('Events') or []:
                message=event.get('DriverMessage') or ''
                if 'terminal_failure_summary; summary=' not in message:continue
                try:summary=json.loads(message.split('summary=',1)[1])
                except (ValueError,IndexError):continue
                allowed={'stage','exception_type','error_type','reason','deadline_seconds','elapsed_seconds','exit_code','signal'}
                if not isinstance(summary,dict) or set(summary)-allowed or any(not isinstance(v,str) or len(v)>160 for v in summary.values()):continue
                summaries.append({'allocation_id':a['ID'],'time':event.get('Time'),'summary':summary})
    if not summaries:return {**(saved_diagnostics(job) or {'job':job,'phase':'terminal_summary_not_recorded','cause':'no_structured_summary_in_retained_task_events','raw_logs_restored':False}),'job_lifecycle':job_lifecycle(api,job),'runtime_state':'not_observed_by_summary; absence of summary is not evidence of a live VM'}
    result={'job':job,'summaries':summaries[-8:],'source':'nomad_server_task_events','raw_logs_restored':False}
    path=diagnostic_path(job);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    st=path.parent.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:raise ValueError('Unsafe diagnostics directory')
    temp=path.with_name(path.name+'.'+str(uuid.uuid4())+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(result,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    return result


def log_cache_path(job,kind):
    if kind not in ['stdout','stderr']:raise ValueError('Invalid log stream')
    return Path.home()/'.local/state/nomad-boxlite-prime/logs'/(__import__('hashlib').sha256(job.encode()).hexdigest()+'.'+kind)

def saved_logs(job,kind):
    base=log_cache_path(job,kind)
    try:
        fd=os.open(str(base)+'.json',os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            st=os.fstat(f.fileno())
            if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:raise ValueError('Unsafe saved log receipt')
            receipt=json.load(f)
        if receipt.get('job')!=job or receipt.get('stream')!=kind:raise ValueError('Saved log identity mismatch')
        name=receipt.get('snapshot_file',base.name+'.log')
        if name!=base.name+'.log':
            prefix=base.name+'.'
            if not isinstance(name,str) or not name.startswith(prefix) or not name.endswith('.log'):raise ValueError('Saved log path mismatch')
            str(uuid.UUID(name[len(prefix):-4]))
        file=base.parent/name;fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW);h=__import__('hashlib').sha256();tail=b'';size=0
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:raise ValueError('Unsafe saved log')
            for block in iter(lambda:f.read(65536),b''):h.update(block);size+=len(block);tail=(tail+block)[-65536:]
        if receipt.get('sha256')!=h.hexdigest() or receipt.get('bytes')!=size:raise ValueError('Saved log digest mismatch')
        return {**receipt,'source':'coordinator_saved_log_snapshot','local_path':str(file),'logs':tail.decode(errors='replace'),'inline_excerpt':size>65536,'raw_logs_restored':False}
    except FileNotFoundError:return None

def read_logs(api,job,kind='stdout'):
    try:
        access=session_access(api,job,derive_auth=False)
        path='/v1/client/fs/logs/'+access['allocation_id']+'?task='+access.get('log_task','vm')+'&type='+kind+'&follow=false&plain=true&origin=start&offset=0'
        base=log_cache_path(job,kind);base.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        st=base.parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:raise ValueError('Unsafe log cache directory')
        temp=Path(str(base)+'.'+str(uuid.uuid4())+'.tmp');h=__import__('hashlib').sha256();size=0;quota=256*1024*1024;complete=True
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as out:
            if isinstance(api,Nomad):
                req=urllib.request.Request(api.origin+path+'&namespace='+NAMESPACE,headers={'X-Nomad-Token':api.token})
                with urllib.request.urlopen(req,timeout=20) as response:
                    started=time.monotonic()
                    while True:
                        block=response.read(65536)
                        if not block:break
                        if size+len(block)>quota or time.monotonic()-started>30:complete=False;break
                        out.write(block);h.update(block);size+=len(block)
            else:
                block=api.call(path,text=True).encode();out.write(block);h.update(block);size=len(block)
            out.flush();os.fsync(out.fileno())
        snapshot=base.name+'.'+str(uuid.uuid4())+'.log'
        os.replace(temp,base.parent/snapshot)
        receipt={'job':job,'allocation_id':access['allocation_id'],'stream':kind,'bytes':size,'sha256':h.hexdigest(),'captured_at':time.time(),'snapshot_file':snapshot,'snapshot_only':True,'complete_snapshot':complete,'retention':'Until explicit coordinator cleanup; no automatic deletion','capture_limit_bytes':quota}
        if not complete:receipt['cause']='log_capture_limit_or_deadline; snapshot incomplete, original may contain more data'
        temp=Path(str(base)+'.'+str(uuid.uuid4())+'.json.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(receipt,f);f.flush();os.fsync(f.fileno())
        os.replace(temp,str(base)+'.json');return saved_logs(job,kind)
    except urllib.error.HTTPError as e:
        saved=saved_logs(job,kind)
        return {'phase':'logs_unavailable','http_status':e.code,'cause':'allocation_logs_expired_or_client_state_missing' if e.code==404 else 'scoped_credential_denied' if e.code==403 else 'nomad_log_read_failed','not_vm_status':True,'saved_snapshot':saved,'terminal_diagnostics':terminal_diagnostics(api,job) if e.code==404 else None}

def cache_failure_logs(api,job,result):
    copies={};barrier={'provider_exited_observed':False}
    if isinstance(api,Nomad):
        try:
            access=session_access(api,job,derive_auth=False)
            path='/v1/client/fs/logs/'+access['allocation_id']+'?task='+access.get('log_task','vm')+'&type=stdout&follow=true&plain=true&origin=start&offset=0'
            deadline=time.monotonic()+8
            for raw in stream_lines(api,path,8):
                if time.monotonic()>deadline:break
                try:e=json.loads(raw)
                except ValueError:continue
                if e.get('event')=='secret_provider_exited':barrier={'provider_exited_observed':True,'exit_code':e.get('exit_code')};break
        except Exception as e:barrier['error_type']=type(e).__name__
    for kind in ['stdout','stderr']:
        try:copies[kind]=read_logs(api,job,kind)
        except Exception as e:copies[kind]={'phase':'log_capture_failed','error_type':type(e).__name__,'not_vm_status':True}
    return {**result,'log_drain_barrier':barrier,'log_snapshots':copies}


def allocations(api, job):
    return api.call('/v1/job/'+urllib.parse.quote(child_job(job), safe='')+'/allocations')


def status(api, job):
    rows=allocations(api,job);current=job_lifecycle(api,job)
    values=[]
    for a in rows:
        row={k:a.get(k) for k in ['ID','ClientStatus','DesiredStatus','JobVersion','TaskStates','RescheduleTracker']}
        terminal=a.get('ClientStatus') in ['complete','failed','lost']
        row['terminal_history']=terminal
        row['older_job_version']=isinstance(a.get('JobVersion'),int) and isinstance(current.get('version'),int) and a['JobVersion']<current['version']
        values.append(row)
    return {'job':job,'job_lifecycle':current,'allocations':values,'runtime_observation':'Nomad history only; native VM/PID is not queried by this API'}





def node_inventory(api):
    # Official Nomad inventory only. Never infer host architecture from an image
    # name or use this snapshot as a capacity reservation / custom scheduler.
    previous=getattr(api,'wait_deadline',None);deadline=time.monotonic()+15
    api.wait_deadline=min(previous,deadline) if previous is not None else deadline
    try:
        rows=api.call('/v1/nodes?resources=true')
        nodes=[]
        for row in rows[:100]:
            if time.monotonic()>=api.wait_deadline:raise TimeoutError('node_inventory_deadline')
            node=api.call('/v1/node/'+str(uuid.UUID(row['ID'])))
            attrs=node.get('Attributes') or {}
            nodes.append({'id':node['ID'],'name':node.get('Name'),'status':node.get('Status'),
                'scheduling_eligibility':node.get('SchedulingEligibility'),'datacenter':node.get('Datacenter'),
                'node_pool':node.get('NodePool'),'host_architecture':attrs.get('cpu.arch'),
                'host_kernel':attrs.get('kernel.name'),'boxlite_api':attrs.get('driver.boxlite.api'),
                'drivers':node.get('Drivers',{}),'resources':node.get('NodeResources'),
                'reserved':node.get('ReservedResources')})
        return {'nodes':nodes,'complete':len(rows)<=100,'at_unix':time.time(),
            'source':'Nomad nodes API and node details','placement':'Nomad constraints/resources; inventory is not a reservation'}
    except urllib.error.HTTPError as e:
        if e.code in (401,403):return {'nodes':[],'complete':False,'phase':'node_inventory_denied' if e.code==403 else 'node_inventory_unauthenticated','http_status':e.code,'required_scope':'node:read' if e.code==403 else None,'credentials_changed':False}
        return {'nodes':[],'complete':False,'phase':'node_inventory_unavailable','http_status':e.code,'credentials_changed':False}
    except (TimeoutError,urllib.error.URLError) as e:
        return {'nodes':[],'complete':False,'phase':'node_inventory_unavailable','error_type':type(e).__name__,'deadline_seconds':15,'credentials_changed':False}
    finally:api.wait_deadline=previous

def policy_catalog(api):
    try:return universal.parse_catalog(api.call('/v1/var/'+universal.POLICY_PATH))
    except urllib.error.HTTPError as e:
        raise ValueError('BoxLite operator budget unavailable: '+NAMESPACE+':'+universal.POLICY_PATH+' HTTP '+str(e.code)+'; no create') from None

def has_local_receipt(job):
    if not universal.is_parent(job):return False
    path=Path.home()/'.local/state/nomad-boxlite-prime/receipts'/(job.removeprefix('worker-')+'.json')
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):return False
            raw=f.read(65537)
        if len(raw)>65536:return False
        d=json.loads(raw)
        return d.get('state')=='dispatched' and d.get('response',{}).get('DispatchedJobID')==job
    except (FileNotFoundError,ValueError,OSError):return False

def capacity_summary(api):
    catalog=policy_catalog(api);started=time.time();nodes={};sessions=[];errors=[]
    try:inventory=node_inventory(api)
    except Exception as e:inventory={'nodes':[],'complete':False,'phase':'node_inventory_unavailable','error_type':type(e).__name__}
    details={n['id']:n for n in inventory.get('nodes',[])}
    for node,limit in catalog['nodes'].items():
        nodes[node]={'limits':limit,'current_namespace_reservations':{'vms':0,'guest_vcpus':0,'guest_ram_mib':0,'reservation_mib':0,'overhead_mib':0,'nomad_cpu_mhz':0}}
    rows=[a for a in api.call('/v1/allocations') if a.get('ClientStatus') in ['pending','running','unknown']]
    for row in rows[:64]:
        try:
            a=api.call('/v1/allocation/'+row['ID']);job=a.get('Job') or {};node=a.get('NodeID');groups=job.get('TaskGroups') or []
            tasks=[t for g in groups if g.get('Name')==a.get('TaskGroup') for t in g.get('Tasks',[]) if t.get('Driver')=='boxlite']
            if not tasks:continue
            if node not in nodes:raise ValueError('Allocation node absent from operator policy')
            for task in tasks:
                cfg=task.get('Config') or {};cpu=cfg.get('guest_cpus');mem=cfg.get('memory_mib');resources=task.get('Resources') or {}
                allocated=(a.get('AllocatedResources') or {}).get('Tasks',{}).get(task['Name'],{})
                reserved=allocated.get('Memory',{}).get('MemoryMB',resources.get('MemoryMB'));mhz=allocated.get('Cpu',{}).get('CpuShares',resources.get('CPU'))
                if any(not isinstance(v,(int,float)) or isinstance(v,bool) or v<0 or int(v)!=v for v in [cpu,mem,reserved,mhz]):raise ValueError('Missing numeric allocation reservation')
                u=nodes[node]['current_namespace_reservations'];u['vms']+=1;u['guest_vcpus']+=int(cpu);u['guest_ram_mib']+=int(mem);u['reservation_mib']+=int(reserved);u['overhead_mib']+=int(reserved-mem);u['nomad_cpu_mhz']+=int(mhz)
            work_state={'state':'unknown','reason_code':'unobserved_or_not_owned','auto_close_enabled':False};owned=has_local_receipt(a['JobID']);state='pending' if a.get('ClientStatus')=='pending' else 'unknown';active=None
            if owned and a.get('ClientStatus')=='running' and a.get('DesiredStatus')=='run':
                try:
                    access=session_access(api,a['JobID']);info=active_session(api,a['JobID'],access);work_state=durable_export.classify(a['JobID'],info);active=any(x.get('state') in ['accepted','running'] for x in info.get('requests',[]));state=info.get('state','unknown')
                except Exception as e:state='unknown';errors.append({'allocation_id':a['ID'],'stage':'session_state','error_type':type(e).__name__})
            sessions.append({'job':a['JobID'],'display_name':job.get('Name',a['JobID']),'allocation_id':a['ID'],'node_id':node,'nomad_state':a.get('ClientStatus'),'desired_state':a.get('DesiredStatus'),'state':state,'work_state':work_state,'auto_close_eligible':False,'local_receipt_verified':owned,'active_request':active,'idle_candidate':owned and state=='ready' and active is False,'reuse_requires_explicit_project_ownership':True,'reservation_mib':sum(int((t.get('Resources') or {}).get('MemoryMB',0)) for t in tasks),'usage':{'cpu_mhz':None,'host_runtime_rss_bytes':None,'guest_mem_used_bytes':None}})
        except Exception as e:errors.append({'allocation_id':row.get('ID'),'stage':'allocation_reservations','error_type':type(e).__name__})
    complete=len(rows)<=64 and not any(e['stage']=='allocation_reservations' for e in errors)
    for node_id,n in nodes.items():
        b=n['limits'];u=n['current_namespace_reservations'];memory=max(0,b['max_reservation_mib']-u['reservation_mib'])
        n['remaining_from_visible_reservations']={'vms':max(0,b['max_vms']-u['vms']),'guest_vcpus':max(0,b['max_guest_cpus']-u['guest_vcpus']),'reservation_mib':memory,'max_guest_ram_for_one_more_vm_mib':max(0,memory-b['memory_overhead_mib'])}
        detail=details.get(node_id,{})
        n['scope']='per_node_not_cluster';n['name']=detail.get('name');n['host_architecture']=detail.get('host_architecture');n['host_kernel']=detail.get('host_kernel')
        resources=detail.get('resources') or {};reserved=detail.get('reserved') or {}
        total_ram=(resources.get('Memory') or {}).get('MemoryMB');total_cpu=(resources.get('Cpu') or {}).get('CpuShares')
        host_ram=(reserved.get('Memory') or {}).get('MemoryMB',0);host_cpu=(reserved.get('Cpu') or {}).get('CpuShares',0)
        if all(type(v) in (int,float) and v>=0 for v in [total_ram,total_cpu,host_ram,host_cpu]):
            n['scheduler_capacity']={'available':True,'allocatable_ram_mib':max(0,total_ram-host_ram),'allocatable_cpu_mhz':max(0,total_cpu-host_cpu),'host_reserved_ram_mib':host_ram,'host_reserved_cpu_mhz':host_cpu,'visible_namespace_used_ram_mib':u['reservation_mib'],'visible_namespace_used_cpu_mhz':u['nomad_cpu_mhz'],'remaining_ram_upper_bound_mib':max(0,total_ram-host_ram-u['reservation_mib']),'remaining_cpu_upper_bound_mhz':max(0,total_cpu-host_cpu-u['nomad_cpu_mhz']),'usage_scope':'visible namespace allocations; not actual CPU/RSS usage or a reservation guarantee'}
        else:n['scheduler_capacity']={'available':False,'reason':inventory.get('phase','node_resources_missing'),'required_scope':inventory.get('required_scope'),'error_type':inventory.get('error_type')}
    return {'namespace':NAMESPACE,'observed_at':started,'completed_at':time.time(),'policy_modify_index':catalog['modify_index'],'nodes':nodes,'sessions':sessions,'errors':errors,'completeness':{'namespace_allocations_complete':complete,'native_orphan_inventory_available':False,'global_free_capacity_confirmed':False},'placement_notice':'Budgets are per node, not a global VM limit. Use a registry-verified image architecture and Nomad constraints; free amd64 capacity cannot run an arm64 image. Do not sum architectures as interchangeable capacity.','scope':'Namespace allocation reservations only; remaining is an upper bound if native/orphan usage is unobserved. Driver admission is authoritative. Idle VMs still reserve resources; never reuse another project session without explicit ownership confirmation.'}

def dispatch(api, request, state_dir, project=None,catalog=None,required_guest_features=()):
    if project is not None:universal.project_slug(project)
    d = validate(request)
    if 'image' not in d:raise ValueError('Legacy profiles are usable for existing sessions only')
    j=universal.build_job(d,catalog if catalog is not None else policy_catalog(api),project,required_guest_features=required_guest_features)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = state_dir/(d['request_id']+'.json')
    # Reserve before the non-repeatable dispatch; no automatic retry on timeout.
    fd = os.open(receipt, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    with os.fdopen(fd,'w') as f:json.dump({'state':'pending','request_id':d['request_id']},f)
    print(json.dumps({'event':'dispatch_reserved','request_id':d['request_id'],'receipt':str(receipt)}),flush=True)
    if 'image' in d:
        response=api.call('/v1/jobs','POST',{'Job':j,'EnforceIndex':True,'JobModifyIndex':0})
        response={'DispatchedJobID':j['ID'],'DisplayName':j['Name'],'spec_id':universal.identity(d),'StateContractVersion':1,**response}
        if catalog is not None and 'placement_inventory' in catalog:
            inv=catalog['placement_inventory'];response['ResourceInventory']={'source':inv.get('source','Nomad nodes API'),'complete':inv.get('complete',False),'accounting_complete':universal.inventory_budget_details(catalog) is not None,'phase':inv.get('phase','observed'),'required_scope':inv.get('required_scope'),'reservation':'compatible node budgets' if universal.inventory_budget_details(catalog) is not None else 'conservative maximum across all operator budgets'}
    else:
        raise ValueError('Legacy profiles are usable for existing sessions only')
    receipt.write_text(json.dumps({'state':'dispatched','response':response})+'\n')
    return {'request_id':d['request_id'],'session_id':d['session_id'],**response}


class SessionReplaced(ValueError):
    def __init__(self,job,rows,original=None):
        ordered=sorted(rows,key=lambda a:(a.get('CreateIndex',0),a.get('CreateTime',0),a['ID']))
        first=next((a for a in ordered if a['ID']==original),ordered[0])
        self.details={'phase':'session_lost','guest_ready':False,'job':job,
            'original_allocation':original or first['ID'],
            'allocations':[{'id':a['ID'],'status':a.get('ClientStatus'),'desired':a.get('DesiredStatus'),
                'reason':a.get('DesiredDescription','')} for a in ordered],
            'cause':'allocation_replaced_after_node_down' if any('node is down' in a.get('DesiredDescription','') for a in ordered) else 'allocation_identity_changed',
            'do_not_retry_prompt':True,'automatic_session_replacement':False,
            'saved_result_help':'result JOB REQUEST_ID --saved uses collected local results without contacting another VM'}
        super().__init__('Original ACP allocation was replaced; no prompt sent to replacement')

def session_access(api,job,derive_auth=True):
    # Public dispatch metadata is authoritative; caller cannot choose another endpoint.
    d=api.call('/v1/job/'+urllib.parse.quote(child_job(job),safe=''))
    if d.get('Namespace')!=NAMESPACE:raise ValueError('Unexpected namespace')
    generic=universal.is_parent(d.get('ID'))
    name=d.get('Meta',{}).get('spec_id') if generic else contract._pm.profile_for_parent(d.get('ParentID'))
    if generic and (not isinstance(name,str) or not __import__('re').fullmatch('[a-f0-9]{64}',name)):raise ValueError('Missing immutable request identity')
    rows=allocations(api,job)
    if len(rows)>1:raise SessionReplaced(job,rows)
    if len(rows)!=1:raise ValueError('Expected one allocation')
    # Existing namespace token deliberately has no node:read. Trust the
    # administrator-registered fixed parent and its inherited native task spec.
    native=[t for g in d.get('TaskGroups',[]) for t in g.get('Tasks',[]) if t.get('Name')=='vm' and t.get('Driver')=='boxlite']
    if len(native)!=1:raise ValueError('Trusted job lacks native BoxLite VM task')
    if generic:
        payload=json.loads(native[0]['Config']['worker_request'])
        if universal.identity(payload)!=name or universal.parent(payload)!=d['ID']:raise ValueError('Job request identity mismatch')
        cfg=native[0]['Config']
        if any(cfg.get(k)!=payload[v] for k,v in [('image','image'),('guest_cpus','cpus'),('memory_mib','memory_mib'),('disk_gib','disk_gib')]):raise ValueError('Job resource or image substitution')
    alloc=api.call('/v1/allocation/'+rows[0]['ID'])
    ports=alloc['AllocatedResources']['Shared']['Ports']
    matches=[p for p in ports if p['Label']=='session']
    if len(matches)==1 and not __import__('ipaddress').ip_address(matches[0]['HostIP']).is_private:raise ValueError('Expected private compute address')
    if len(matches)!=1:raise ValueError('Expected one LAN session port')
    access=auth.access(d['Meta']['session_id'],name) if derive_auth else {'session_id':str(uuid.UUID(d['Meta']['session_id']))}
    access['port']=contract._pm.port(matches[0]['Value'])
    access['host']=matches[0]['HostIP']
    access['allocation_id']=rows[0]['ID']
    access['log_task']='vm' if generic else 'fixed-prime'
    return access

def endpoint(op,access,body=None):
    # Never take host, URL, shell, port or upstream path from request data.
    if op not in ('status','prompt','cancel','close','artifacts','artifact-transfer','answer','request-index','exchange') and not __import__('re').fullmatch(r'result/[0-9a-f-]{36}',op):
        raise ValueError('Unsupported guest operation')
    req=urllib.request.Request('http://'+access['host']+':'+str(contract._pm.port(access['port']))+'/'+op,
        method='GET' if op=='status' or op.startswith('result/') else 'POST',
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+access['token'],'X-Session-ID':access['session_id'],
                 'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=130 if op in ('artifact-transfer','exchange') else 10) as r:return json.loads(r.read(131073))

def active_session(api,job,access):
    rows=allocations(api,job)
    if len(rows)>1 or (len(rows)==1 and access.get('allocation_id') and rows[0]['ID']!=access['allocation_id']):
        raise SessionReplaced(job,rows,access.get('allocation_id'))
    if len(rows)!=1 or rows[0]['ClientStatus']!='running' or rows[0]['DesiredStatus']!='run':
        raise ValueError('Expected one running allocation; never redispatch a follow-up')
    info=endpoint('status',access)
    if info.get('session_id')!=access['session_id'] or info.get('state')=='closed':
        raise ValueError('Wrong or closed guest session')
    return info


def placement_state(api,job):
    """Latest real scheduler evidence, not a prediction from free capacity."""
    rows=api.call('/v1/job/'+urllib.parse.quote(child_job(job),safe='')+'/evaluations')
    latest=max(rows,key=lambda e:(e.get('CreateIndex',0),e.get('ModifyIndex',0)),default={})
    failed=latest.get('FailedTGAllocs') or {};dimensions={};filtered=0;exhausted=0
    for metrics in failed.values():
        filtered+=int(metrics.get('NodesFiltered',0));exhausted+=int(metrics.get('NodesExhausted',0))
        for name,count in (metrics.get('DimensionExhausted') or {}).items():
            if name in ('cpu','memory','disk','network','cores'):dimensions[name]=dimensions.get(name,0)+int(count)
    blocked=bool(failed)
    reason='scheduler_capacity_exhausted' if exhausted or dimensions else 'scheduler_constraints_unsatisfied' if filtered else 'scheduler_placement_blocked' if blocked else 'scheduler_evaluation_pending'
    return {'phase':'pending_resources' if blocked else 'scheduling_pending','reason_code':reason,'placement_blocked':blocked,'evaluation_id':latest.get('ID'),'evaluation_status':latest.get('Status'),'exhausted_dimensions':dimensions,'nodes_filtered':filtered,'nodes_exhausted':exhausted,'guest_ready':False,'job_not_stopped':True,'do_not_retry_prompt':True,'meaning':'No VM readiness established. This is scheduler placement, not failed guest execution; retain the same job unless explicitly cancelled.'}

def checked_status(api,job):
    full=allocations(api,job);nomad=status(api,job)
    if len(full)>1 and nomad.get('job_lifecycle',{}).get('stop') is not True:return {**SessionReplaced(job,full).details,'nomad':nomad}
    rows=nomad['allocations']
    location=None
    if len(rows)==1:
        location='Compute node: ~/Library/Application Support/BoxLiteNomad/sessions/'+str(uuid.UUID(rows[0]['ID']))+'/'
    out={'nomad':nomad,'guest_ready':False,'diagnostics':location,
         'diagnostic_files':['diagnostic.json','bridge.jsonl']}
    failures={41:'registry_token_failed',42:'registry_image_download_failed',43:'boxlite_execution_failed',44:'guest_runtime_setup_failed',47:'execution_or_cleanup_unconfirmed',48:'persistent_runtime_busy',49:'existing_vm_active_or_uncertain',50:'unsafe_runtime_state',51:'aggregate_profile_budget_exhausted',52:'admission_state_unresolved',54:'session_lifetime_expired'}
    durable=[]
    for a in rows:
        for state in (a.get('TaskStates') or {}).values():
            for e in state.get('Events') or []:
                if e.get('Type')=='Terminated' and e.get('ExitCode') in failures:
                    durable.append({'allocation_id':a['ID'],'exit_code':e['ExitCode'],'failure_phase':failures[e['ExitCode']]})
    policy_failures=[]
    for a in rows:
        for state in (a.get('TaskStates') or {}).values():
            for event in state.get('Events') or []:
                message=' '.join(str(event.get(k,'')) for k in ['DriverError','Message','DisplayMessage'])
                match=__import__('re').search(r'boxlite_policy_error code=([a-z_]+) variable=([^ ]+)',message)
                if match:policy_failures.append({'allocation_id':a['ID'],'layer':'admission','code':match[1],'variable':match[2],'no_vm_created':True})
    if policy_failures:out['launch_failure']=policy_failures
    out['terminal_diagnostics']=terminal_diagnostics(api,job)
    lifecycle=nomad.get('job_lifecycle',{})
    if lifecycle.get('stop') is True:
        terminal=all(a.get('ClientStatus') in ['complete','failed','lost'] for a in rows)
        out['phase']='job_stopped' if terminal else 'job_stopping'
        out['guest']={'state':'not_active_in_nomad' if terminal else 'shutdown_requested','native_vm_state':'unverified','reason':'Current job Stop=true; retained failed/DesiredStatus=run allocation is historical, not a request to keep this VM alive'}
        out['action']='No retry/cancel/purge inferred; verify native ownership separately only if an orphan is suspected'
        out['saved_results']='result JOB REQUEST_ID --saved; previously exported artifacts remain on coordinator'
        return out
    out['driver_events']=[{'allocation_id':a['ID'],'message':e.get('DriverMessage'),'details':e.get('Details')} for a in rows for st in (a.get('TaskStates') or {}).values() for e in st.get('Events',[]) if e.get('Type')=='Driver'][-24:]
    if rows and all(a['ClientStatus'] in ('complete','failed','lost') for a in rows) and not durable:
        out['phase']='completed' if all(a['ClientStatus']=='complete' for a in rows) else 'failed'
        out['saved_results']='result JOB REQUEST_ID --saved; artifacts collected before finish remain on coordinator'
        return out
    if durable:
        out['launch_failure']=durable
        out['diagnostic_source']='Nomad server TaskStates; retained after client log GC while job history exists; detailed redacted logs at Mac path'
    if rows and any(a['ClientStatus'] in ('complete','failed','lost') for a in rows):
        out['phase']='terminal'
        out['guest']={'state':'not_running','reason':'No live guest; inspect retained Mac logs; do not redispatch blindly'}
        return out
    if not rows or any(a['ClientStatus']!='running' for a in rows):
        out['phase']='pending_resources'
        out['guest']={'state':'pending_scheduling','reason':'Not running; placement/resources must be satisfied before guest readiness'}
        try:out.update(placement_state(api,job))
        except Exception as e:out.update({'phase':'observation_unavailable','reason_code':'placement_diagnostics_unavailable','guest_state_unknown':True,'placement_diagnostics':{'error_type':type(e).__name__,'http_status':getattr(e,'code',None)}})
        return out
    try:
        info=active_session(api,job,session_access(api,job))
    except urllib.error.HTTPError as e:
        if e.code!=503:raise
        out['phase']='starting'
        out['guest']={'state':'starting_or_unavailable','http_status':503,
                      'reason':'Host gateway answered; guest/ACP readiness NOT established'}
        return out
    except (urllib.error.URLError,TimeoutError,ConnectionError) as e:
        out['phase']='starting'
        out['guest']={'state':'gateway_unreachable','error_class':type(e).__name__,
                      'reason':'Allocation running but ACP gateway not reachable; guest readiness unknown. Check same job and driver events; do not redispatch.'}
        return out
    out['guest']=info
    out['work_state']=durable_export.classify(job,info)
    out['phase']=info.get('state','starting')
    out['guest_ready']=bool(info.get('acp_session_id') and info.get('state') in ('ready','busy'))
    return out


def saved_result_path(request_id):
    return Path.home()/'.local/state/nomad-boxlite-prime/results'/(str(uuid.UUID(request_id))+'.json')

def save_result(job,request_id,result):
    child_job(job);request_id=str(uuid.UUID(request_id))
    if result.get('request_id')!=request_id or len(json.dumps(result).encode())>120000:
        raise ValueError('Result identity or size mismatch')
    result=observability.result_view(result)
    path=saved_result_path(request_id);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    st=path.parent.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:
        raise ValueError('Unsafe coordinator result directory')
    temp=path.with_name(path.name+'.'+str(uuid.uuid4())+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'job':job,'result':result},f);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()
    return {**result,'local_result_path':str(path),'result_source':'live_and_saved'}

def load_saved_result(job,request_id):
    child_job(job);path=saved_result_path(request_id)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or not stat.S_ISREG(st.st_mode):
            raise ValueError('Unsafe saved result')
        raw=f.read(131073)
    if len(raw)>131072:raise ValueError('Saved result size limit')
    d=json.loads(raw)
    if d.get('job')!=job or d.get('result',{}).get('request_id')!=str(uuid.UUID(request_id)):
        raise ValueError('Saved result binding mismatch')
    return {**observability.result_view(d['result']),'local_result_path':str(path),'result_source':'saved_no_remote_access'}


def collect_artifacts(api,job,paths):
    for name in paths:contract.relative_file(name)
    if not 1<=len(paths)<=16 or len(set(paths))!=len(paths):raise ValueError('Artifact path count')
    access=session_access(api,job);info=active_session(api,job,access)
    if info.get('state')!='ready':raise ValueError('Collect only when guest idle')
    data=endpoint('artifacts',access,{'paths':paths})
    if data.get('session_id')!=access['session_id'] or data.get('acp_session_id')!=info['acp_session_id']:
        raise ValueError('Artifact session mismatch')
    if [f.get('path') for f in data.get('files',[])]!=paths:raise ValueError('Artifact path mismatch')
    checked=[];total=0
    for f in data['files']:
        raw=base64.b64decode(f['base64'],validate=True);total+=len(raw)
        if total>65536 or len(raw)!=f['size'] or __import__('hashlib').sha256(raw).hexdigest()!=f['sha256']:
            raise ValueError('Artifact content mismatch')
        checked.append((f['path'],raw,f['sha256']))
    root=Path.home()/'.local/state/nomad-boxlite-prime/artifacts'/str(uuid.UUID(access['session_id']))/str(uuid.uuid4())
    base=root.parent.parent;base.mkdir(parents=True,exist_ok=True,mode=0o700)
    root.parent.mkdir(mode=0o700,exist_ok=True);root.mkdir(mode=0o700)
    manifest=[]
    for name,raw,digest in checked:
        target=root/'files'/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw)
        manifest.append({'path':name,'sha256':digest,'bytes':len(raw),'local_path':str(target)})
    report={'job':job,'session_id':access['session_id'],'acp_session_id':info['acp_session_id'],'artifacts':manifest,'local_directory':str(root),'files_directory':str(root/'files'), 'layout':'Downloaded paths are below files/; manifest.json is at local_directory'}
    (root/'manifest.json').write_text(json.dumps(report)+'\n');os.chmod(root/'manifest.json',0o600)
    return report


def stream_lines(api,path,timeout):
    sep='&' if '?' in path else '?'
    req=urllib.request.Request(api.origin+path+sep+'namespace='+NAMESPACE,headers={'X-Nomad-Token':api.token})
    with urllib.request.urlopen(req,timeout=timeout) as response:
        for raw in response:
            if len(raw)>1048576:raise ValueError('Event frame limit')
            yield raw

def transient_observation_error(error):
    if isinstance(error,urllib.error.HTTPError):return error.code in (408,429,500,502,503,504)
    return isinstance(error,(urllib.error.URLError,TimeoutError,ConnectionError,http.client.IncompleteRead,http.client.RemoteDisconnected))

def wait_session(api,job,until='ready',request_id=None,deadline_seconds=300,after=0):
    if isinstance(deadline_seconds,bool) or not isinstance(deadline_seconds,(int,float)) or not __import__('math').isfinite(deadline_seconds) or deadline_seconds<0:raise ValueError('Finite nonnegative wait deadline required')
    observer={'after':after,'deadline':time.monotonic()+deadline_seconds}
    previous=getattr(api,'wait_deadline',None);api.wait_deadline=observer['deadline'];attempt=0
    try:
        while True:
            left=observer['deadline']-time.monotonic()
            if left<=0:return {'phase':'wait_deadline','cursor':observer['after'],'do_not_retry_prompt':True,'session_not_stopped':True}
            cursor_before=observer['after']
            try:
                result=_wait_session_once(api,job,until,request_id,left,observer['after'],observer)
                if result.get('phase')=='transport_disconnected':raise http.client.RemoteDisconnected('read-only stream closed')
                return result
            except Exception as e:
                if not transient_observation_error(e):raise
                if observer['after']>cursor_before:attempt=0
                attempt+=1;left=observer['deadline']-time.monotonic()
                if left<=0:return {'phase':'wait_deadline','cursor':observer['after'],'do_not_retry_prompt':True,'session_not_stopped':True}
                context={'job':job,'request_id':request_id,'allocation_id':observer.get('allocation_id'),'cursor':observer['after'],'error_type':type(e).__name__,'http_status':getattr(e,'code',None)}
                if attempt>=8:return {'phase':'observation_unavailable',**context,'cause':'read_only_reconnect_limit','guest_state_unknown':True,'do_not_retry_prompt':True,'session_not_stopped':True}
                delay=min(5,.5*2**(attempt-1),left)
                print(json.dumps({'event':'observation_temporarily_unavailable',**context,'attempt':attempt,'retry_in_seconds':delay,'read_only':True,'prompt_replayed':False}),flush=True)
                time.sleep(delay)
    finally:api.wait_deadline=previous

def _wait_session_once(api,job,until='ready',request_id=None,deadline_seconds=300,after=0,observer=None):
    # Snapshot once, then native blocking streams. Never retry a prompt.
    child_job(job);deadline=observer['deadline'] if observer is not None else time.monotonic()+deadline_seconds
    if job_lifecycle(api,job).get('stop') is True:return checked_status(api,job)
    def remaining():
        left=deadline-time.monotonic()
        if left<=0:raise TimeoutError('wait_deadline; no prompt replay or VM replacement')
        return left
    rows=allocations(api,job)
    if not rows:
        pending=placement_state(api,job)
        if pending['placement_blocked']:return pending
        print(json.dumps({'event':'scheduling_pending','job':job,'reason_code':pending['reason_code'],'deadline_seconds':remaining(),'prompt_replayed':False}),flush=True)
        topics=urllib.parse.urlencode([('topic','Allocation:'+child_job(job)),('topic','Evaluation:'+child_job(job)),('topic','Job:'+child_job(job))])
        if observer and observer.get('nomad_index'):topics+='&index='+str(observer['nomad_index'])
        first_packet=True
        for raw in stream_lines(api,'/v1/event/stream?'+topics,remaining()):
            packet=json.loads(raw)
            if observer is not None and isinstance(packet.get('Index'),int):observer['nomad_index']=packet['Index']
            if not first_packet and not packet.get('Events'):continue
            first_packet=False
            if job_lifecycle(api,job).get('stop') is True:return checked_status(api,job)
            rows=allocations(api,job)
            if rows:break
            pending=placement_state(api,job)
            if pending['placement_blocked']:return pending
        if not rows:return {'phase':'observation_unavailable','reason_code':'scheduler_stream_closed','guest_state_unknown':True,'job_not_stopped':True,'do_not_retry_prompt':True}
    if len(rows)!=1:raise ValueError('Original allocation unavailable or replaced')
    if rows[0].get('ClientStatus') in ['complete','failed','lost']:return cache_failure_logs(api,job,{'phase':'failed','reason_code':'allocation_terminal_before_gateway','diagnostics':checked_status(api,job),'do_not_retry_prompt':True})
    allocation_id=rows[0]['ID'];log_task='vm' if universal.is_parent(job) else 'fixed-prime'
    if observer is not None:
        if observer.get('allocation_id',allocation_id)!=allocation_id:return {'phase':'session_lost','cause':'allocation_identity_changed','do_not_retry_prompt':True}
        observer['allocation_id']=allocation_id
    allocation=api.call('/v1/allocation/'+allocation_id)
    if (allocation.get('TaskStates') or {}).get(log_task,{}).get('State')!='running':
        # Subscribe first, snapshot once again to cover the subscribe/snapshot race.
        native=stream_lines(api,'/v1/event/stream?topic=Allocation:'+allocation_id,remaining())
        first_packet=True
        for raw in native:
            packet=json.loads(raw)
            if not first_packet and not packet.get('Events'):continue
            first_packet=False
            current=api.call('/v1/allocation/'+allocation_id)
            state=(current.get('TaskStates') or {}).get(log_task,{}).get('State')
            if current.get('ClientStatus') in ['complete','failed','lost']:return cache_failure_logs(api,job,{'phase':'failed','reason_code':'allocation_terminal_before_gateway','diagnostics':checked_status(api,job),'do_not_retry_prompt':True})
            if state=='running':break
    access=session_access(api,job)
    if access['allocation_id']!=allocation_id:return {'phase':'session_lost','cause':'allocation_identity_changed','do_not_retry_prompt':True}
    snapshot=checked_status(api,job)
    if snapshot.get('guest_ready') and until=='ready':return snapshot
    if snapshot.get('guest_ready') and request_id:
        current=next((r for r in snapshot.get('guest',{}).get('requests',[]) if r['request_id']==request_id),None)
        if current and current.get('state') in ['completed','cancelled','failed_no_retry','uncertain_no_retry']:return save_result(job,request_id,endpoint('result/'+str(uuid.UUID(request_id)),access))
    if snapshot.get('phase') in ['job_stopped','job_stopping']:return snapshot
    if snapshot.get('phase') in ['failed','terminal','session_lost']:return cache_failure_logs(api,job,snapshot)
    if snapshot.get('phase')=='completed':return snapshot
    # Log follow streams bootstrap readiness/failure before the gateway exists.
    # Read from start so listener events cannot be lost between snapshot and subscribe.
    path='/v1/client/fs/logs/'+access['allocation_id']+'?task='+access.get('log_task','fixed-prime')+'&type=stdout&follow=true&plain=true&origin=start&offset=0'
    for raw in stream_lines(api,path,remaining()):
        try:e=json.loads(raw)
        except ValueError:continue
        if e.get('event')=='acp_bridge_failed':return cache_failure_logs(api,job,{'phase':'failed','cause':e,'do_not_retry_prompt':True})
        if e.get('event')=='gateway_listening':break
        # Legacy bridge has no subscription endpoint: fail explicitly, never hide polling.
        if e.get('event')=='guest_session_ready' and not after:
            if until=='ready':return {'phase':'ready','guest_ready':True,'guest':e,'transport':'legacy_log_event'}
            raise ValueError('Legacy session lacks result subscription; upgrade only a future session')
    url='http://'+access['host']+':'+str(access['port'])+'/events?'+urllib.parse.urlencode({'after':after,'deadline':max(1,int(remaining()))})
    req=urllib.request.Request(url,headers={'Authorization':'Bearer '+access['token'],'X-Session-ID':access['session_id']})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=remaining()) as response:
        for raw in response:
            e=json.loads(raw)
            if observer is not None:
                if observer.get('epoch') and e.get('epoch') and observer['epoch']!=e['epoch']:return {'phase':'replay_gap','cause':'event_epoch_changed','cursor':observer['after'],'do_not_retry_prompt':True}
                if e.get('epoch'):observer['epoch']=e['epoch']
                if e.get('event') not in ['snapshot','wait_deadline','replay_gap'] and isinstance(e.get('sequence'),int):
                    if e['sequence']<=observer['after']:continue
                    observer['after']=e['sequence']
            cursor=Path.home()/'.local/state/nomad-boxlite-prime/cursors'/str(uuid.UUID(access['session_id']))
            cursor.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(cursor,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f:json.dump({'job':job,'allocation_id':access['allocation_id'],'sequence':observer['after'] if observer is not None else e.get('sequence'),'epoch':e.get('epoch')},f)
            print(json.dumps({'event':'worker_event','data':e}),flush=True)
            if e.get('event') in ['replay_gap','wait_deadline']:return {'phase':e['event'],'cursor':e.get('sequence'),'do_not_retry_prompt':True,'snapshot':e.get('snapshot')}
            if e.get('event') in ['acp_transport_failed','acp_bridge_failed'] or (e.get('event')=='result' and e.get('outcome') not in ['session_closed']):return cache_failure_logs(api,job,{'phase':'failed','cause':e,'do_not_retry_prompt':True})
            if e.get('event')=='question':return {'phase':'input_required','question':e,'cursor':e['sequence'],'asynchronous_wakeup':False}
            if until=='ready' and (e.get('event')=='guest_session_ready' or e.get('snapshot',{}).get('state') in ['ready','busy']):return {'phase':'ready','guest_ready':True,'cursor':e['sequence']}
            if until=='result' and e.get('event')=='request_terminal' and e.get('request_id')==request_id:
                return save_result(job,request_id,endpoint('result/'+str(uuid.UUID(request_id)),access))
    return {'phase':'transport_disconnected','do_not_retry_prompt':True}

def activity(api,job,deadline_seconds=1,after=0,follow=False,include_messages=False):
    if job_lifecycle(api,job).get('stop') is True:return checked_status(api,job)
    access=session_access(api,job);snapshot_at=time.time();state=active_session(api,job,access);observed_state=state.get('state')
    requests=[{k:r.get(k) for k in ['request_id','state']} for r in state.get('requests',[])]
    context={k:access[k] for k in ['allocation_id','session_id']}
    latest=None;last_tool=None;recent=[];messages=[];gap=False;sequence=after
    redactor=auth.secure_logs.Redactor([access.get('token'),getattr(api,'token',None)])
    url='http://'+access['host']+':'+str(access['port'])+'/events?'+urllib.parse.urlencode({'after':after,'deadline':deadline_seconds})
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+access['token'],'X-Session-ID':access['session_id']})
    started=time.monotonic()
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=deadline_seconds+2) as response:
        for raw in response:
            if len(raw)>1048576:raise ValueError('Event frame limit')
            e=json.loads(raw);sequence=e.get('sequence',sequence)
            candidate=e.get('snapshot',{}).get('state')
            if candidate in ['ready','busy','closed']:observed_state=candidate
            if e.get('event')=='replay_gap':gap=True
            if e.get('event') in ['snapshot','wait_deadline','replay_gap']:continue
            timestamp=e.get('time',e.get('timestamp'))
            if e.get('event') in observability.EVENTS or e.get('event') in ['assistant_delta','guest_session_ready','result']:
                latest={'event':e.get('event'),'sequence':sequence}
                if observability.number(timestamp):latest.update(timestamp=timestamp,age_seconds=max(0,round(time.time()-timestamp,3)))
            if include_messages and e.get('event') in ['assistant_delta','agent_progress'] and isinstance(e.get('text'),str):
                if e.get('event')=='assistant_delta' or e.get('message_kind')=='plan':
                    message={'event':e['event'],'request_id':observability.identifier(e.get('request_id')),'sequence':sequence,'text':redactor.text(e['text'][:4096]),'channel':'authenticated_agent_message'}
                    if e.get('event')=='agent_progress':message['message_kind']='plan'
                    messages.append(message);messages=messages[-16:]
                    if follow:print(json.dumps({'event':'agent_message','data':message}),flush=True)
            p=observability.project(e,context)
            if p:
                if observability.number(timestamp):p['timestamp']=timestamp
                p['sequence']=sequence;recent.append(p);recent=recent[-32:]
                if e.get('event')=='progress':last_tool=p
                if follow:print(json.dumps({'event':'activity','data':p}),flush=True)
            if time.monotonic()-started>deadline_seconds+1:break
    return {'job':job,'session_id':access['session_id'],'allocation_id':access['allocation_id'],'state':observed_state,'work_state':dict(durable_export.classify(job,state),basis='guest_status_snapshot',observed_at=snapshot_at),'status_snapshot_at':snapshot_at,
            'requests':requests,'running_elapsed_ms':state.get('running_elapsed_ms') if observability.number(state.get('running_elapsed_ms')) else None,'active_request_id':observability.identifier(state.get('active_request_id')),'recent_messages':messages,'last_observed_event':latest,'last_observed_tool':last_tool,'recent_metadata':recent,'cursor':sequence,'replay_gap':gap,
            'scope':'Read-only status snapshot plus retained event metadata; no prompts, text, files or tool arguments. No model-wait/hang inferred from absent events; old guests may lack tool IDs/durations.'}

def upload_brief(access,path):
    raw=Path(path).read_bytes()
    if not raw or len(raw)>1048576:raise ValueError('Brief quota1MiB')
    raw.decode('utf8');digest=__import__('hashlib').sha256(raw).hexdigest()
    for offset in range(0,len(raw),49152):
        chunk=raw[offset:offset+49152]
        endpoint('artifact-transfer',access,{'operation':'brief','sha256':digest,'offset':offset,'base64':base64.b64encode(chunk).decode(),'final':offset+len(chunk)==len(raw)})
    return digest

def file_digest(path):
    h=__import__('hashlib').sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

def collect_streamed_artifacts(api,job,paths):
    for name in paths:contract.relative_file(name)
    if not 1<=len(paths)<=4096 or len(set(paths))!=len(paths):raise ValueError('Artifact path quota4096')
    access=session_access(api,job);info=active_session(api,job,access)
    if info.get('state')!='ready':raise ValueError('Collect only when guest idle')
    if info.get('protocol_version',1)<2:return collect_artifacts(api,job,paths)
    manifest=[]
    for start in range(0,len(paths),64):
        d=endpoint('artifact-transfer',access,{'operation':'manifest','paths':paths[start:start+64]})
        if d.get('session_id')!=access['session_id'] or d.get('acp_session_id')!=info.get('acp_session_id'):raise ValueError('Artifact session changed')
        manifest.extend(d['files'])
    if [f['path'] for f in manifest]!=paths or sum(f['size'] for f in manifest)>1073741824:raise ValueError('Manifest quota or identity')
    transfer=__import__('hashlib').sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    root=Path.home()/'.local/state/nomad-boxlite-prime/artifacts'/str(uuid.UUID(access['session_id']))/transfer
    # Every parent is owned, directory-only, no symlinks. Never extract archives.
    base=Path.home()/'.local/state/nomad-boxlite-prime'
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    for directory in [base/'artifacts',root.parent,root,root/'files']:
        directory.mkdir(mode=0o700,exist_ok=True)
        st=directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:raise ValueError('Unsafe artifact directory')
    report={'job':job,'session_id':access['session_id'],'acp_session_id':info.get('acp_session_id'),'local_directory':str(root),'files_directory':str(root/'files'), 'layout':'Downloaded paths are below files/; manifest.json is at local_directory','artifacts':[]}
    for item in manifest:
        if type(item['size']) is not int or not 0<=item['size']<=268435456:raise ValueError('Artifact file quota256MiB')
        target=root/'files'/item['path'];parent=root/'files'
        for part in Path(item['path']).parts[:-1]:
            parent=parent/part;parent.mkdir(mode=0o700,exist_ok=True)
            if not stat.S_ISDIR(parent.lstat().st_mode) or parent.is_symlink():raise ValueError('Unsafe output path')
        partial=target.with_name(target.name+'.part')
        if target.exists():
            if target.is_symlink() or file_digest(target)!=item['sha256']:raise ValueError('Existing artifact changed')
        else:
            fd=os.open(partial,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'r+b') as stream:
                st=os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise ValueError('Unsafe partial')
                offset=stream.seek(0,2)
                while offset<item['size']:
                    d=endpoint('artifact-transfer',access,{'operation':'chunk','path':item['path'],'identity':item['identity'],'offset':offset})
                    raw=base64.b64decode(d['base64'],validate=True)
                    if d['offset']!=offset or not raw or len(raw)>49152 or __import__('hashlib').sha256(raw).hexdigest()!=d['sha256']:raise ValueError('Chunk integrity')
                    stream.write(raw);stream.flush();offset+=len(raw)
                stream.seek(0);h=__import__('hashlib').sha256()
                for block in iter(lambda:stream.read(1048576),b''):h.update(block)
                if offset!=item['size'] or h.hexdigest()!=item['sha256']:raise ValueError('Artifact integrity; partial retained')
                os.fsync(stream.fileno())
            os.replace(partial,target)
        report['artifacts'].append({'path':item['path'],'bytes':item['size'],'sha256':item['sha256'],'local_path':str(target),'guest_executable':item.get('guest_executable',False)})
    p=root/'manifest.json';p.write_text(json.dumps(report)+'\n');p.chmod(0o600);return report

# Reuse the installed SkyPilot staging policy, with stricter no-symlink handling.
STAGE_EXCLUDE=__import__('re').compile(r'(^|/)(\.env[^/]*|node_modules|\.cache|dist|\.beads|\.repowise|\.internal|output|\.git|\.ssh|\.aws|\.kube|\.config|\.venv|__pycache__)(/|$)|\.(pem|key)$|(^|/)(\.npmrc|\.pypirc|\.netrc|id_rsa|id_ed25519|kubeconfig|credentials)$|^\.(brief|worker-report|coordinator-report)\.md')

def workspace_open(root,name):
    parts=Path(name).parts;fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        out=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        if not stat.S_ISREG(os.fstat(out).st_mode):os.close(out);raise ValueError('Nonregular source')
        return os.fdopen(out,'rb')
    finally:os.close(fd)

def workspace_manifest(source,paths):
    import subprocess,hashlib,re
    root=Path(source).absolute()
    if root.is_symlink() or not root.is_dir():raise ValueError('Source must be an explicitly selected real directory')
    root=root.resolve();names=[]
    for name in paths:
        if name!='.':contract.relative_file(name)
    git=root/'.git'
    if git.exists():
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(Path.home()),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
        p=subprocess.run(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(root),'ls-files','-co','--exclude-standard','-z'],env=env,capture_output=True,timeout=20)
        if p.returncode:raise ValueError('Cannot enumerate selected Git worktree; no broad fallback')
        names=[n.decode() for n in p.stdout.split(b'\0') if n]
    else:
        for directory,dirs,files in os.walk(root,followlinks=False):
            rel=Path(directory).relative_to(root)
            dirs[:]=[n for n in dirs if not STAGE_EXCLUDE.search(str(rel/n)) and not (Path(directory)/n).is_symlink()]
            names.extend(str(rel/f) for f in files)
    selected=lambda n:any(p=='.' or n==p or n.startswith(p.rstrip('/')+'/') for p in paths)
    result=[];excluded=0;total=0
    sensitive=re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b|\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}')
    for name in sorted(set(names)):
        if not selected(name):continue
        if STAGE_EXCLUDE.search(name):excluded+=1;continue
        contract.relative_file(name);path=root/name
        if any((root/Path(*Path(name).parts[:i])).is_symlink() for i in range(1,len(Path(name).parts)+1)):raise ValueError('Symlink refused: '+name)
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode):raise ValueError('Nonregular file refused: '+name)
        if st.st_size>268435456:raise ValueError('Source file quota: '+name)
        h=hashlib.sha256();tail=b''
        with workspace_open(root,name) as f:
            for block in iter(lambda:f.read(1048576),b''):
                if sensitive.search(tail+block):raise ValueError('Sensitive-looking content refused: '+name)
                h.update(block);tail=block[-256:]
        after=path.stat()
        if (st.st_size,st.st_mtime_ns,st.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('Source changed during snapshot: '+name)
        result.append({'path':name,'size':st.st_size,'sha256':h.hexdigest(),'executable':bool(st.st_mode&0o100)});total+=st.st_size
        if len(result)>4096 or total>1073741824:raise ValueError('Workspace quota4096files/1GiB')
    if not result:raise ValueError('No eligible selected files')
    return root,result,excluded

def upload_workspace(api,job,source,paths,destination=None,apply=False):
    import hashlib
    root,files,excluded=workspace_manifest(source,paths)
    if destination is None:destination='snapshots/'+hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:24]
    contract.relative_file(destination)
    transfer=hashlib.sha256(json.dumps({'destination':destination,'files':files},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    report={'transfer_id':transfer,'source':str(root),'destination':destination,'files':len(files),'bytes':sum(f['size'] for f in files),'excluded':excluded,'conflict_policy':'fail unless identical','snapshot_not_mount':True}
    if not apply:return {**report,'dry_run':True,'manifest':files}
    access=session_access(api,job);info=active_session(api,job,access)
    if info.get('protocol_version',1)<2 or not info.get('capabilities',{}).get('workspace_upload'):raise ValueError('This guest has no workspace upload capability; never replace it implicitly')
    def send(data):return endpoint('artifact-transfer',access,{'transfer_id':transfer,**data})
    send({'operation':'upload-start','destination':destination,'files':len(files),'bytes':report['bytes']})
    for item in files:
        path=root/item['path'];current=path.lstat()
        if not stat.S_ISREG(current.st_mode) or path.is_symlink() or file_digest(path)!=item['sha256']:raise ValueError('Source changed since manifest')
        r=send({'operation':'upload-file',**item});offset=r['offset']
        with workspace_open(root,item['path']) as f:
            f.seek(offset)
            while offset<item['size']:
                raw=f.read(49152)
                if not raw:raise ValueError('Source shortened during transfer')
                r=send({'operation':'upload-chunk','path':item['path'],'offset':offset,'base64':base64.b64encode(raw).decode()});offset+=len(raw)
                if r.get('offset')!=offset:raise ValueError('Upload acknowledgement mismatch')
    committed=send({'operation':'upload-commit'})
    if not committed.get('committed'):raise ValueError('Upload commit unconfirmed')
    saved=Path.home()/'.local/state/nomad-boxlite-prime/uploads'/str(uuid.UUID(access['session_id']))/(transfer+'.json');saved.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(saved,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump({**report,'manifest':files},f)
    return {**report,'committed':True,'local_manifest':str(saved)}

def main(argv=None, api=None):
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='op',required=True)
    q=sub.add_parser('models',help='Read configured operator model choices; no credential or provider URL is returned')
    q=sub.add_parser('nodes',help='Official Nomad node inventory/host architecture; requires existing node:read permission')
    q=sub.add_parser('resources',help='Discover approved limits; optional existing namespace allocation snapshot');q.add_argument('--live',action='store_true')
    for op in ['request']:
        q=sub.add_parser(op,help='Start worker with explicit image, CPU and RAM' if op=='request' else 'Offline explainable resource selection; does not dispatch')
        q.add_argument('--project',help='Optional explicit public project label for Nomad display name; immutable job ID is unchanged')
        q.add_argument('--model',help='Optional operator-approved model ID; omitted uses the operator default, never a provider URL')
        q.add_argument('--image',help='Explicit immutable image reference @sha256')
        q.add_argument('--cpus',type=int,help='Explicit guest vCPU count')
        q.add_argument('--memory-mib',type=int,help='Explicit guest RAM in MiB')
        q.add_argument('--disk-gib',type=int,help='Optional disk size; default comes only from operator policy')
        q.add_argument('--node-id',help='Optional explicit approved compute NodeID pin; omitted lets Nomad place by architecture/resources')
        q.add_argument('--architecture',choices=['arm64','amd64'],help='Verified Linux image architecture; required for automatic Nomad placement')
        q.add_argument('--input',help='Optional bounded initialization JSON; no host commands or endpoint settings')
    for op in ['status','cancel','finish']:
        q=sub.add_parser(op);q.add_argument('job')
    q=sub.add_parser('result');q.add_argument('job');q.add_argument('request_id');q.add_argument('--saved',action='store_true')
    q=sub.add_parser('artifacts');q.add_argument('job');q.add_argument('--paths',nargs='+',required=True)
    q=sub.add_parser('continue');q.add_argument('--from-job',required=True);q.add_argument('--input');q.add_argument('--brief-file')
    q=sub.add_parser('wait',help='Event subscription with bounded replay; no status polling');q.add_argument('job');q.add_argument('--until',choices=['ready','result'],default='ready');q.add_argument('--request-id');q.add_argument('--deadline',type=int,default=300);q.add_argument('--after',type=int,default=0)
    q=sub.add_parser('upload',help='Selected filtered worktree snapshot; default preview, never a live mount');q.add_argument('job');q.add_argument('--source',required=True);q.add_argument('--paths',nargs='+',default=['.']);q.add_argument('--destination');q.add_argument('--apply',action='store_true')
    q=sub.add_parser('answer',help='Answer an actual ACP form elicitation, never a permission grant');q.add_argument('job');q.add_argument('--input',required=True)
    q=sub.add_parser('interrupt',help='Cancel the active turn without stopping the session VM');q.add_argument('job')
    q=sub.add_parser('activity',help='Read-only metadata from current status and bounded existing events; never executes or resumes a turn');q.add_argument('job');q.add_argument('--deadline',type=int,default=1);q.add_argument('--after',type=int,default=0);q.add_argument('--follow',action='store_true');q.add_argument('--messages',action='store_true',help='Include intended agent output/plan text from the authenticated stream; never thoughts or raw tool arguments')
    q=sub.add_parser('export-ack',help='Explicit atomic acknowledgement of all declared result outputs verified on existing coordinator disk; never auto-close or upload');q.add_argument('job');q.add_argument('request_id');q.add_argument('--manifest',required=True);q.add_argument('--declared-paths',nargs='+',required=True);q.add_argument('--all-outputs-declared',action='store_true',required=True)
    q=sub.add_parser('diagnostics',help='Server-retained terminal summary and coordinator saved fallback; not raw-log recovery');q.add_argument('job');q.add_argument('--saved',action='store_true')
    q=sub.add_parser('registries',help='Read operator-configured registry origin, never credentials')
    q=sub.add_parser('images',help='Read registry catalog/tags/manifests, without project mapping');q.add_argument('--registry',help='Optional override; otherwise operator.json registry_origin');q.add_argument('--repository');q.add_argument('--workers-only',action='store_true');q.add_argument('--architecture',choices=['arm64','amd64','aarch64','x86_64'])
    q=sub.add_parser('logs',help='Export retained Nomad logs to protected coordinator cache; --saved works after GC only for previously captured snapshots');q.add_argument('job');q.add_argument('--stream',choices=['stdout','stderr'],default='stdout');q.add_argument('--saved',action='store_true')
    q=sub.add_parser('exchange-upload',help='Optional S3 selected workspace transfer; no config/keys needed for normal direct upload');q.add_argument('job');q.add_argument('--source',required=True);q.add_argument('--paths',nargs='+',required=True);q.add_argument('--destination');q.add_argument('--apply',action='store_true')
    q=sub.add_parser('exchange-export',help='Explicit all-declared terminal outputs to optional S3; never closes VM');q.add_argument('job');q.add_argument('request_id');q.add_argument('--paths',nargs='+',required=True);q.add_argument('--all-outputs-declared',action='store_true',required=True);q.add_argument('--snapshot',action='store_true',help='Selected-file snapshot only, not a terminal agent result; ready/no active request required')
    q=sub.add_parser('exchange-fetch',help='Retrieve S3 committed result using receipt after VM stop, without Nomad/guest');q.add_argument('--receipt',required=True)
    q=sub.add_parser('hold',help='Explicit bounded session hold; no automatic close');q.add_argument('job');q.add_argument('--seconds',type=float);q.add_argument('--release',action='store_true')
    args=p.parse_args(argv)
    if getattr(args,'project',None) is not None:universal.project_slug(args.project)
    if args.op=='diagnostics' and args.saved:return saved_diagnostics(args.job) or {'phase':'diagnostics_unavailable','cause':'no_saved_summary'}
    if args.op=='models':
        import model_policy
        policy=auth.protected_json(Path.home()/'.config/nomad-boxlite-prime/model-policy.json');model_policy.select_model({},policy)
        return {'default_model':policy['default_model'],'allowed_models':policy['allowed_models'],'scope':'Operator model IDs only; no gateway or credentials'}
    if args.op=='registries':return {'registries':[configured_registry_origin()],'source':'operator_config','credentials_returned':False}
    if args.op=='images':return {**registry_images(configured_registry_origin(args.registry),args.repository,args.workers_only,args.architecture),'registry_source':'explicit_argument' if args.registry else 'operator_config'}
    if args.op=='logs' and args.saved:return saved_logs(args.job,args.stream) or {'phase':'logs_unavailable','cause':'no_previously_captured_log_snapshot; raw logs cannot be restored after GC'}

    if args.op=='upload' and not args.apply:return upload_workspace(None,args.job,args.source,args.paths,args.destination,False)
    if args.op=='result' and args.saved:return load_saved_result(args.job,args.request_id)
    if args.op=='exchange-fetch':
        import exchange_cli
        return exchange_cli.run(sys.modules[__name__],args)
    api=api or Nomad()
    if args.op in ('exchange-upload','exchange-export'):
        import exchange_cli
        return exchange_cli.run(sys.modules[__name__],args,api)

    if args.op=='nodes':return node_inventory(api)
    if args.op=='activity':
        if not 1<=args.deadline<=3600 or args.after<0:raise ValueError('Activity deadline1s..1h and nonnegative cursor')
        try:return activity(api,args.job,args.deadline,args.after,args.follow,args.messages)
        except (urllib.error.URLError,TimeoutError,auth.SeedUnavailable) as e:return {'job':args.job,'phase':'activity_unavailable','error_type':type(e).__name__,'not_evidence_of_idle_or_hang':True}
    if args.op=='diagnostics':return terminal_diagnostics(api,args.job)
    if args.op=='logs':return read_logs(api,args.job,args.stream)
    if args.op=='resources':
        return capacity_summary(api) if args.live else policy_catalog(api)
    if args.op=='upload':return upload_workspace(api,args.job,args.source,args.paths,args.destination,True)
    if args.op=='export-ack':
        child_job(args.job);access=session_access(api,args.job);info=active_session(api,args.job,access)
        print(json.dumps({'event':'export_verification_started','request_id':str(uuid.UUID(args.request_id)),'storage_kind':'coordinator-host','auto_close_enabled':False}),flush=True)
        return durable_export.acknowledge(args.job,info,args.request_id,args.manifest,args.declared_paths,args.all_outputs_declared)
    if args.op=='hold':
        import idle_policy
        access=session_access(api,args.job);active_session(api,args.job,access)
        return idle_policy.hold(args.job,access['session_id'],args.seconds,args.release)
    if args.op=='status':return checked_status(api,args.job)
    if args.op=='wait':
        if args.deadline<1 or args.deadline>604800:raise ValueError('Wait deadline1s..7days is caller wait, not VM lifetime')
        if args.until=='result' and not args.request_id:raise ValueError('Result wait requires request ID')
        try:return wait_session(api,args.job,args.until,args.request_id,args.deadline,args.after)
        except auth.SeedUnavailable as e:return cache_failure_logs(api,args.job,{'phase':'coordinator_auth_unavailable','cause':str(e),'do_not_retry_prompt':True,'session_not_stopped':True})
        except TimeoutError:return {'phase':'wait_deadline','deadline_seconds':args.deadline,'do_not_retry_prompt':True,'session_not_stopped':True}
    if args.op in ['answer','interrupt']:
        access=session_access(api,args.job);active_session(api,args.job,access)
        return endpoint('answer' if args.op=='answer' else 'cancel',access,json.loads(Path(args.input).read_text()) if args.op=='answer' else {})
    if args.op=='artifacts':return collect_streamed_artifacts(api,args.job,args.paths)
    if args.op=='result':
        try:access=session_access(api,args.job);info=active_session(api,args.job,access)
        except SessionReplaced as e:
            try:saved=load_saved_result(args.job,args.request_id)
            except FileNotFoundError:raise e
            return {**saved,'allocation_warning':e.details}
        rid=str(uuid.UUID(args.request_id));result=endpoint('result/'+rid,access)
        if result.get('session_id')!=access['session_id'] or result.get('acp_session_id')!=info.get('acp_session_id'):
            raise ValueError('Result session binding mismatch')
        return save_result(args.job,rid,result)
    if args.op in ['cancel','finish']:
        child_job(args.job)
        closed=False
        try:
            access=session_access(api,args.job);current=active_session(api,args.job,access)
            if args.op=='finish':
                if current.get('state')=='busy':raise ValueError('Active turn: interrupt then wait/export before finish; VM retained')
                records=current.get('requests',[])
                if current.get('requests_truncated'):
                    records=[];offset=0
                    while offset is not None:
                        page=endpoint('request-index',access,{'offset':offset});records.extend(page['requests']);offset=page['next_offset']
                for record in records:
                    if record.get('state') in ['completed','cancelled','failed_no_retry','uncertain_no_retry']:
                        rid=str(uuid.UUID(record['request_id']));saved=endpoint('result/'+rid,access);save_result(args.job,rid,saved)
                        if saved.get('result_file') and current.get('state')=='ready':collect_streamed_artifacts(api,args.job,[saved['result_file']])
            response=endpoint('close' if args.op=='finish' else 'cancel',access,{})
            closed=args.op=='finish' and response.get('state')=='closed'
        except Exception as e:
            if args.op=='finish':raise ValueError('Pre-close result preservation not confirmed; VM retained. Inspect diagnostics; cancel is explicit stop without export guarantee') from None
            # Explicit cancel retains authoritative Nomad stop fallback.
        if closed:
            # Let the foreground chain finish its own finally/VM cleanup before
            # deregistration. Never race a successful ACP close with SIGTERM.
            try:
                for raw in stream_lines(api,'/v1/event/stream?topic=Allocation:'+access['allocation_id'],10):
                    e=json.loads(raw)
                    if any(v.get('Payload',{}).get('Allocation',{}).get('ClientStatus') in ['complete','failed','lost'] for v in e.get('Events',[])):break
            except (TimeoutError,urllib.error.URLError):pass
        return {'job':args.job,'stop':api.call('/v1/job/'+urllib.parse.quote(args.job,safe=''),'DELETE')}
    if args.op=='continue':
        access=session_access(api,args.from_job);session_info=active_session(api,args.from_job,access)
        d=json.loads(Path(args.input).read_text()) if args.input else {}
        if args.brief_file:
            if session_info.get('protocol_version',1)<2:raise ValueError('Legacy session has no file brief transport; use a future session, do not replace active work')
            if 'prompt' in d:raise ValueError('Choose inline prompt or brief file')
            d['brief_sha256']=upload_brief(access,args.brief_file)
        if set(d)-{'request_id','prompt','brief_sha256'} or (not d.get('brief_sha256') and (not isinstance(d.get('prompt'),str) or not 0<len(d['prompt'].encode())<=49152)):
            raise ValueError('Follow-up accepts request_id and prompt (48KiB) or --brief-file (1MiB)')
        if session_info.get('protocol_version',1)<2 and len(d.get('prompt','').encode())>4096:raise ValueError('Active legacy guest retains4KiB prompt contract; no live session replacement')
        d['request_id']=str(uuid.UUID(d.get('request_id',str(uuid.uuid4()))))
        receipts=Path.home()/'.local/state/nomad-boxlite-prime/turns'
        receipts.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Reserve before HTTP. A timeout may mean the turn ran: no blind replay.
        fd=os.open(receipts/(d['request_id']+'.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'job':args.from_job,'request_id':d['request_id'],'state':'pending'},f)
        print(json.dumps({'event':'turn_reserved','request_id':d['request_id']}),flush=True)
        return endpoint('prompt',access,d)
    if args.image or args.cpus is not None or args.memory_mib is not None or (args.input and 'image' in json.loads(Path(args.input).read_text())):
        d={} if args.input is None else json.loads(Path(args.input).read_text())
        for k,v in [('image',args.image),('cpus',args.cpus),('memory_mib',args.memory_mib),('model',args.model),('architecture',args.architecture)]:
            if v is not None:d[k]=v
        import model_policy
        model_policy.select_model(d,auth.protected_json(Path.home()/'.config/nomad-boxlite-prime/model-policy.json'))
        image_resolution=None
        if isinstance(api,Nomad) and d.get('architecture'):
            image_resolution=resolve_platform_image(d['image'],d['architecture']);d['image']=image_resolution['resolved_image']
        catalog=policy_catalog(api)
        if args.node_id:d['node_id']=args.node_id
        if 'node_id' not in d and 'architecture' not in d:raise ValueError('Use --architecture from verified image metadata for Nomad placement; no default NodeID')
        if 'node_id' not in d:
            catalog['placement_inventory']=node_inventory(api)
        policy=universal.request_policy(catalog,d.get('node_id'),d.get('architecture'))
        if args.disk_gib is not None:d['disk_gib']=args.disk_gib
        d.setdefault('disk_gib',policy['default_disk_gib'])
        d.setdefault('request_id',str(uuid.uuid4()));d.setdefault('session_id',str(uuid.uuid4()))
        d=universal.validate(d);auth.access(d['session_id'],universal.identity(d))
        result=dispatch(api,d,Path.home()/'.local/state/nomad-boxlite-prime/receipts',args.project,catalog=catalog,required_guest_features=(image_resolution or {}).get('required_guest_features',()))
        if image_resolution:result['ImageResolution']=image_resolution
        return result
    raise ValueError('New workers require explicit --image, --cpus and --memory-mib; profiles are legacy session compatibility only')



if __name__=='__main__':
    try:
        result=main();print(json.dumps(result))
        if result.get('phase') in ('storage_unavailable','exchange_failed'):sys.exit(6)
        if result.get('task_result',{}).get('state')=='incomplete':sys.exit(7)
    except SessionReplaced as e:
        print(json.dumps(e.details),file=sys.stderr);sys.exit(5)
    except auth.SeedUnavailable:
        print(json.dumps({'error':'OpenBao dispatcher seed unavailable; no dispatch/retry','fail_closed':True}),file=sys.stderr);sys.exit(4)
    except (FileNotFoundError,ValueError) as e:
        print(json.dumps({'error':str(e),'activation_or_review_required':True}),file=sys.stderr);sys.exit(2)
    except urllib.error.HTTPError as e:
        print(json.dumps({'error':('Nomad' if urllib.parse.urlsplit(str(e.url)).hostname in ('127.0.0.1','localhost') else 'Guest')+' HTTP '+str(e.code),'do_not_automatically_retry':True}),file=sys.stderr);sys.exit(3)
    except Exception as e:
        print(json.dumps({'error':type(e).__name__,'outcome':'uncertain_do_not_retry'}),file=sys.stderr);sys.exit(3)
