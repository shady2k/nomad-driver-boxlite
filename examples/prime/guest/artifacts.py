"""Guest-only, bounded read of selected workspace regular files. No host paths."""
import base64,hashlib,json,os,stat,sys
ROOT='/workspace/repo';MAX_TOTAL=65536

def relative(value):
    if not isinstance(value,str) or not value or len(value)>256 or '\\' in value or '\0' in value:
        raise ValueError('invalid_artifact_path')
    parts=value.split('/')
    if any(v in ('','.','..','.git') for v in parts):raise ValueError('invalid_artifact_path')
    return parts

def export(paths,root=ROOT):
    if not isinstance(paths,list) or not 1<=len(paths)<=16 or len(set(paths))!=len(paths):raise ValueError('artifact_path_count')
    parts=[relative(p) for p in paths];files=[];total=0
    rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name,segments in zip(paths,parts):
            fd=os.dup(rootfd)
            try:
                for part in segments[:-1]:
                    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
                f=os.open(segments[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                with os.fdopen(f,'rb') as stream:
                    st=os.fstat(stream.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_TOTAL-total:raise ValueError('artifact_size_or_type')
                    raw=stream.read(MAX_TOTAL-total+1)
                total+=len(raw)
                if total>MAX_TOTAL:raise ValueError('artifact_total_limit')
                files.append({'path':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode()})
            finally:os.close(fd)
    finally:os.close(rootfd)
    return {'files':files,'total_bytes':total}


# Chunked binary transport. Archives/bundles remain opaque; never extract/execute.
def secure_open(name,root=ROOT):
    segments=relative(name);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in segments[:-1]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        out=os.open(segments[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        if not stat.S_ISREG(os.fstat(out).st_mode):os.close(out);raise ValueError('regular_files_only')
        return out
    finally:os.close(fd)

def fingerprint(stream,limit):
    s=os.fstat(stream.fileno())
    if s.st_size>limit:raise ValueError('file_quota')
    h=hashlib.sha256()
    for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    t=os.fstat(stream.fileno())
    if (s.st_size,s.st_mtime_ns,s.st_ino)!=(t.st_size,t.st_mtime_ns,t.st_ino):raise ValueError('artifact_changed')
    return {'size':s.st_size,'sha256':h.hexdigest(),'guest_executable':bool(s.st_mode&0o100),'identity':[str(s.st_ino),str(s.st_size),str(s.st_mtime_ns)]}

def operate(d,root=ROOT,policy=None):
    policy=policy or {'max_artifact_paths':4096,'max_artifact_file_bytes':268435456,'max_artifact_total_bytes':1073741824,'chunk_bytes':49152,'max_prompt_bytes':1048576}
    op=d.get('operation')
    if op in ('s3-pull','s3-push'):return s3_transfer(d,root,policy)
    if isinstance(op,str) and op.startswith('upload-'):return upload(d,root,policy)
    if op=='manifest':
        names=d['paths']
        if not isinstance(names,list) or not 1<=len(names)<=128 or len(set(names))!=len(names):raise ValueError('manifest_page_limit')
        files=[]
        for name in names:
            with os.fdopen(secure_open(name,root),'rb') as f:files.append({'path':name,**fingerprint(f,policy['max_artifact_file_bytes'])})
        if sum(f['size'] for f in files)>policy['max_artifact_total_bytes']:raise ValueError('total_quota')
        return {'files':files}
    if op=='chunk':
        if type(d['offset']) is not int or d['offset']<0:raise ValueError('offset')
        with os.fdopen(secure_open(d['path'],root),'rb') as f:
            st=os.fstat(f.fileno())
            if [str(st.st_ino),str(st.st_size),str(st.st_mtime_ns)]!=d['identity']:raise ValueError('artifact_changed')
            f.seek(d['offset']);raw=f.read(policy['chunk_bytes'])
            if os.fstat(f.fileno()).st_mtime_ns!=st.st_mtime_ns:raise ValueError('artifact_changed')
        return {'offset':d['offset'],'base64':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest(),'eof':d['offset']+len(raw)==st.st_size}
    if op=='brief':
        import re
        digest=d['sha256'];offset=d['offset']
        if not re.fullmatch('[a-f0-9]{64}',digest) or type(offset) is not int or offset<0:raise ValueError('brief_identity')
        raw=base64.b64decode(d['base64'],validate=True)
        if len(raw)>policy['chunk_bytes'] or offset+len(raw)>policy['max_prompt_bytes']:raise ValueError('brief_quota')
        directory=os.path.join(root,'.nomad-briefs');os.makedirs(directory,mode=0o700,exist_ok=True)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            f=os.open(digest,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(f,'r+b') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('brief_type')
                stream.seek(0,2);size=stream.tell()
                if offset<size:
                    stream.seek(offset)
                    if stream.read(len(raw))!=raw:raise ValueError('brief_conflict')
                elif offset==size:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                else:raise ValueError('brief_offset')
                if d.get('final'):
                    stream.seek(0);data=stream.read(policy['max_prompt_bytes']+1)
                    if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('brief_hash')
                    data.decode('utf-8')
            return {'offset':offset+len(raw),'sha256':digest,'complete':bool(d.get('final'))}
        finally:os.close(fd)
    raise ValueError('unsupported_artifact_operation')


def mkdir_safe(root,parts):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts:
            try:os.mkdir(part,0o700,dir_fd=fd)
            except FileExistsError:pass
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        return fd
    except Exception:os.close(fd);raise

def upload(d,root,policy):
    import re,shutil
    transfer=d['transfer_id']
    if not isinstance(transfer,str) or not re.fullmatch('[a-f0-9]{64}',transfer):raise ValueError('upload_transfer_id')
    folder=os.path.join(root,'.nomad-transfers',transfer)
    fd=mkdir_safe(root,['.nomad-transfers',transfer]);os.close(fd)
    statefile=os.path.join(folder,'state.json')
    def statewrite(s):
        fd=os.open(statefile,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:json.dump(s,f);f.flush();os.fsync(f.fileno())
    op=d['operation']
    if op=='upload-start':
        dest=d['destination'];relative(dest)
        state={'destination':dest,'expected_files':d['files'],'expected_bytes':d['bytes'],'files':{}}
        if type(d['files']) is not int or not 1<=d['files']<=policy['max_artifact_paths'] or type(d['bytes']) is not int or not 0<=d['bytes']<=policy['max_artifact_total_bytes']:raise ValueError('upload_quota')
        if os.path.exists(statefile):
            with os.fdopen(os.open(statefile,os.O_RDONLY|os.O_NOFOLLOW)) as f:old=json.load(f)
            if any(old[k]!=state[k] for k in ['destination','expected_files','expected_bytes']):raise ValueError('upload_conflict')
            return {'started':True,'resumed':True}
        statewrite(state);return {'started':True}
    with os.fdopen(os.open(statefile,os.O_RDONLY|os.O_NOFOLLOW)) as f:s=json.load(f)
    if op=='upload-file':
        name=d['path'];relative(name)
        if type(d['size']) is not int or not 0<=d['size']<=policy['max_artifact_file_bytes'] or not re.fullmatch('[a-f0-9]{64}',d['sha256']):raise ValueError('upload_file_quota')
        meta={'path':name,'size':d['size'],'sha256':d['sha256'],'executable':bool(d.get('executable',False))}
        if name in s['files'] and s['files'][name]!=meta:raise ValueError('upload_file_conflict')
        s['files'][name]=meta
        if len(s['files'])>s['expected_files'] or sum(v['size'] for v in s['files'].values())>s['expected_bytes']:raise ValueError('upload_manifest_quota')
        statewrite(s);fd=mkdir_safe(folder,['files']+relative(name)[:-1])
        try:
            out=os.open(relative(name)[-1],os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                st=os.fstat(out)
                if not stat.S_ISREG(st.st_mode) or st.st_size>d['size']:raise ValueError('upload_file_type')
                return {'offset':st.st_size}
            finally:os.close(out)
        finally:os.close(fd)
    if op=='upload-chunk':
        if s.get('committed'):raise ValueError('committed_transfer_immutable')
        name=d['path'];meta=s['files'][name];offset=d['offset'];raw=base64.b64decode(d['base64'],validate=True)
        if type(offset) is not int or offset<0 or len(raw)>policy['chunk_bytes'] or offset+len(raw)>meta['size']:raise ValueError('upload_chunk_quota')
        fd=mkdir_safe(folder,['files']+relative(name)[:-1])
        try:
            out=os.open(relative(name)[-1],os.O_RDWR|os.O_NOFOLLOW,dir_fd=fd)
            with os.fdopen(out,'r+b') as f:
                f.seek(0,2);size=f.tell()
                if offset<size:
                    f.seek(offset)
                    if f.read(len(raw))!=raw:raise ValueError('upload_chunk_conflict')
                elif offset==size:f.write(raw);f.flush();os.fsync(f.fileno())
                else:raise ValueError('upload_chunk_offset')
                return {'offset':offset+len(raw)}
        finally:os.close(fd)
    if op=='upload-commit':
        files=sorted(s['files'].values(),key=lambda f:f['path'])
        if len(files)!=s['expected_files'] or sum(v['size'] for v in files)!=s['expected_bytes']:raise ValueError('upload_manifest_incomplete')
        canonical=json.dumps({'destination':s['destination'],'files':files},sort_keys=True,separators=(',',':')).encode()
        if hashlib.sha256(canonical).hexdigest()!=transfer:raise ValueError('upload_manifest_hash')
        # Preflight every destination and hash before writing anything.
        for meta in files:
            with os.fdopen(secure_open('files/'+meta['path'],folder),'rb') as f:
                got=fingerprint(f,policy['max_artifact_file_bytes'])
                if got['size']!=meta['size'] or got['sha256']!=meta['sha256']:raise ValueError('upload_hash')
            dest=s['destination']+'/'+meta['path'];fd=mkdir_safe(root,relative(dest)[:-1])
            try:
                try:existing=os.open(relative(dest)[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                except FileNotFoundError:continue
                with os.fdopen(existing,'rb') as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or fingerprint(f,policy['max_artifact_file_bytes'])['sha256']!=meta['sha256']:raise ValueError('destination_conflict_no_overwrite')
            finally:os.close(fd)
        s['committed']=True;statewrite(s)
        for meta in files:
            dest=s['destination']+'/'+meta['path'];fd=mkdir_safe(root,relative(dest)[:-1]);src=mkdir_safe(folder,['files']+relative(meta['path'])[:-1])
            try:
                out=os.open(relative(meta['path'])[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=src)
                try:os.fchmod(out,0o700 if meta['executable'] else 0o600)
                finally:os.close(out)
                # Atomic no-overwrite publication on the same workspace FS.
                try:os.link(relative(meta['path'])[-1],relative(dest)[-1],src_dir_fd=src,dst_dir_fd=fd,follow_symlinks=False)
                except FileExistsError:pass  # Exact existing content passed preflight above.
            finally:os.close(fd);os.close(src)
        return {'committed':True,'transfer_id':transfer,'destination':s['destination'],'files':len(files),'bytes':s['expected_bytes']}
    raise ValueError('upload_operation')

def s3_multipart_push(d,root,policy):
    import urllib.request,urllib.parse,tempfile,fcntl
    plan=d['multipart']
    if plan.get('complete'):return {'transferred':True,'size':d['size'],'sha256':d['sha256'],'resumed':True}
    name=d['path'];relative(name);origin=urllib.parse.urlsplit(d['origin'])
    if origin.scheme!='https' or not origin.hostname:raise ValueError('s3_origin')
    folder=os.path.join(root,'.nomad-transfers','s3-output');fd=mkdir_safe(root,['.nomad-transfers','s3-output']);os.close(fd)
    checkpoint=os.path.join(folder,hashlib.sha256((name+'\0'+d['sha256']).encode()).hexdigest()+'.json')
    lock=os.open(checkpoint+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            fd=os.open(checkpoint,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd) as f:state=json.load(f)
        except FileNotFoundError:state={'upload_id':plan['upload_id'],'parts':{}}
        if state['upload_id']!=plan['upload_id']:raise ValueError('s3_multipart_upload_identity')
        with os.fdopen(secure_open(name,root),'rb') as stream:
            proof=fingerprint(stream,policy['max_artifact_file_bytes'])
            if proof['size']!=d['size'] or proof['sha256']!=d['sha256']:raise ValueError('s3_source_changed')
            resumed=0
            for part in plan['parts']:
                u=urllib.parse.urlsplit(part['put_url'])
                if u.scheme!='https' or u.netloc!=origin.netloc or u.username or u.password:raise ValueError('s3_origin')
                stream.seek((part['number']-1)*plan['part_bytes']);raw=stream.read(part['size']);sha=hashlib.sha256(raw).hexdigest()
                if len(raw)!=part['size']:raise ValueError('s3_source_short')
                previous=state['parts'].get(str(part['number']));remote=part.get('remote')
                if previous and remote and previous=={'sha256':sha,'etag':remote['etag'],'size':remote['size']} and len(raw)==remote['size']:resumed+=1;continue
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self,*a,**k):raise ValueError('s3_redirect')
                req=urllib.request.Request(part['put_url'],method='PUT',data=raw,headers={'Content-Length':str(len(raw))})
                with urllib.request.build_opener(NoRedirect).open(req,timeout=30) as response:etag=response.headers.get('ETag');response.read(1024)
                if not etag or len(etag)>256:raise ValueError('s3_part_etag')
                state['parts'][str(part['number'])]={'sha256':sha,'etag':etag,'size':len(raw)}
                fd,tmp=tempfile.mkstemp(prefix='.s3-part-',dir=folder)
                with os.fdopen(fd,'w') as f:json.dump(state,f);f.flush();os.fsync(f.fileno())
                os.replace(tmp,checkpoint);fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
        return {'transferred':True,'size':d['size'],'sha256':d['sha256'],'resumed_parts':resumed}
    except Exception:raise ValueError('s3_multipart_failed_no_url_logged') from None
    finally:fcntl.flock(lock,fcntl.LOCK_UN);os.close(lock)

def s3_transfer(d,root,policy):
    if d['operation']=='s3-push' and 'multipart' in d:return s3_multipart_push(d,root,policy)
    import urllib.request,urllib.parse
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**k):raise ValueError('s3_redirect_refused')
    u=urllib.parse.urlsplit(d['url']);origin=urllib.parse.urlsplit(d['origin'])
    if u.scheme!='https' or u.netloc!=origin.netloc or origin.scheme!='https' or u.username or u.password:raise ValueError('s3_origin')
    name=d['path'];relative(name)
    try:
        if d['operation']=='s3-pull':
            r=upload({**d,'operation':'upload-file'},root,policy);offset=r['offset']
            if offset==d['size']:
                with os.fdopen(secure_open('files/'+name,os.path.join(root,'.nomad-transfers',d['transfer_id'])),'rb') as staged:
                    proof=fingerprint(staged,policy['max_artifact_file_bytes'])
                if proof['sha256']!=d['sha256']:raise ValueError('s3_staged_hash')
                return {'transferred':True,'size':offset,'sha256':proof['sha256'],'resumed':True}
            # Read from the beginning to hash the complete signed object. Existing
            # chunk conflict checks verify any already staged prefix.
            with urllib.request.build_opener(NoRedirect).open(d['url'],timeout=30) as response:
                offset=0;h=hashlib.sha256()
                while True:
                    raw=response.read(policy['chunk_bytes'])
                    if not raw:break
                    if offset+len(raw)>d['size']:raise ValueError('s3_size')
                    h.update(raw);upload({**d,'operation':'upload-chunk','offset':offset,'base64':base64.b64encode(raw).decode()},root,policy);offset+=len(raw)
            if offset!=d['size'] or h.hexdigest()!=d['sha256']:raise ValueError('s3_hash')
            return {'transferred':True,'size':offset,'sha256':h.hexdigest()}
        if d['operation']=='s3-push':
            with os.fdopen(secure_open(name,root),'rb') as f:
                proof=fingerprint(f,policy['max_artifact_file_bytes'])
                if proof['size']!=d['size'] or proof['sha256']!=d['sha256']:raise ValueError('s3_source_changed')
                f.seek(0);req=urllib.request.Request(d['url'],data=f,method='PUT',headers={'Content-Length':str(d['size'])})
                with urllib.request.build_opener(NoRedirect).open(req,timeout=30) as response:response.read(1024)
            return {'transferred':True,'size':d['size'],'sha256':d['sha256']}
        raise ValueError('s3_operation')
    except Exception:raise ValueError('s3_transfer_failed_no_url_logged') from None

if __name__=='__main__':
    try:
        d=json.loads(sys.stdin.read(131073))
        policy=json.load(open('/opt/worker/session-policy.json'))
        print(json.dumps(operate(d,policy=policy) if 'operation' in d else export(d['paths'])))
    except Exception as e:
        print(json.dumps({'error':str(e)}));sys.exit(2)
