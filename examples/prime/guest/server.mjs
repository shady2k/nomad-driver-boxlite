// JSON transport over the existing foreground BoxLite stdin/stdout. No listener.
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {createInterface} from 'node:readline';
import {createAcpRuntime,createFileSessionStore,createAgentRegistry} from 'acpx/runtime';
import {Session,runtimeErrorMetadata,selectAgentModel} from './session.mjs';
const rl=createInterface({input:process.stdin});
const policy=JSON.parse(readFileSync('/opt/worker/session-policy.json','utf8'));
if(!policy.provider||!policy.default_model||!Array.isArray(policy.allowed_models)||!policy.allowed_models.includes(policy.default_model))throw Error('Operator provider/model policy required');
let first=true,resolveInput,session,timer,spawned=false,stopping=false;
const inputReady=new Promise(resolve=>resolveInput=resolve);
const ready=inputReady.then(async input=>{
  const model=selectAgentModel(policy,input.model);
  const agent=['/usr/bin/python3','/opt/worker/guest.py','--acp-proxy','/usr/local/bin/prime-agent','--offline','--mode','acp','--daemon-socket','/tmp/nomad-prime.sock',
    '--provider',policy.provider,'--model',model,
    '--no-context-files','--no-skills','--no-prompt-templates',
    '--system-prompt','Keep this conversation alive between messages. Send concise user-facing stage updates before long operations and after meaningful results when possible, without private reasoning or periodic heartbeat messages. A synchronous tool may prevent updates while it runs. Delegate only when the coordinator task explicitly authorizes it; do not create extra children or hidden retries. If clarification is needed, ask a concise question in your final response and stop this turn; the coordinator answers with the next turn. No direct coordinator callback is available.'];
  const traceProcess=(action,details={})=>console.log(JSON.stringify({event:'acp_trace',layer:'acp_runtime',timestamp:Date.now()/1000,session_id:input.session_id,request_id:session?.activeRequestId,acp_session_id:session?.handle?.backendSessionId,action,operation:'agent_process',observation:'process_lifecycle',rpc_id_available:false,...details}));
  const runtime=createAcpRuntime({cwd:'/workspace/repo',
    sessionStore:createFileSessionStore({stateDir:'/root/.acpx/nomad-persistent'}),
    agentRegistry:createAgentRegistry({overrides:{prime:agent}}),
    permissionMode:'deny-all',nonInteractivePermissions:'deny',fs:false,terminal:false,timeoutMs:60000,elicitationModes:['form'],
    processLifecycle:{onBeforeSpawn(){if(spawned)throw Error('ACP reconnect forbidden');spawned=true;traceProcess('send',{status:'starting'});},onSpawned(p){traceProcess('receive',{status:'running',pid:p.pid});},onSpawnFailed(f){traceProcess('error',runtimeErrorMetadata(f.error));},onExit(p){traceProcess('disconnect',{status:'exited',exit_code:p.exitCode,signal:['SIGTERM','SIGKILL','SIGINT','SIGABRT','SIGSEGV','SIGHUP','SIGPIPE'].includes(p.signal)?p.signal:null});}},
    agentProcessEnvAllow:['PATH','HOME','PI_OFFLINE','PRIME_AGENT_CODING_AGENT_DIR',
      'PRIME_AGENT_DAEMON_SOCKET','PRIME_AGENT_KERNEL_PYTHON','LLM_API_KEY'],agentProcessEnvBlock:['VAULT_TOKEN','NOMAD_TOKEN','BOXLITE_DISPATCHER_SEED']});
  session=new Session(runtime,input.token,input.session_id,{model,idleMs:policy.idle_ms,maxMs:policy.maximum_ms,turnTimeoutMs:policy.turn_timeout_ms,maxPromptBytes:policy.max_prompt_bytes,maxResultBytes:policy.max_result_bytes,stateDir:'/root/.acpx/nomad-results',onEvent:e=>console.log(JSON.stringify(e))});await session.initialize();
  console.log(JSON.stringify({event:'guest_session_ready',...session.status()}));
  timer=setInterval(()=>session.tick().then(()=>{if(session.closed)finishTransport();}).catch(()=>stop('lifecycle_error')),1000);
});
rl.on('line',async line=>{
  if(first){first=false;try{resolveInput(JSON.parse(line));}catch{process.exitCode=2;finishTransport();}return;}
  let m;
  try{
    if(Buffer.byteLength(line)>131072)throw Error('too_large');
    m=JSON.parse(line);if(!/^[0-9a-f-]{36}$/.test(m.rpc_id))throw Error('invalid_rpc');
    await ready;
    let status=200,body;
    if(m.method==='GET' && m.path==='/status')body=session.status();
    else if(m.method==='GET' && /^\/result\/[0-9a-f-]{36}$/.test(m.path))body=session.result(m.path.slice(8));
    else if(m.method==='POST' && ['/artifacts','/artifact-transfer'].includes(m.path)) {
      if(session.status().state!=='ready' || (m.path==='/artifacts' && Object.keys(m.body || {}).some(k=>k!=='paths')))throw Error('not_ready');
      const out=spawnSync('/usr/bin/python3',['-I','/opt/worker/artifacts.py'],{
        input:JSON.stringify(m.body),encoding:'utf8',timeout:120000,maxBuffer:131072,
        env:{PATH:'/usr/bin:/bin',HOME:'/root'}});
      if(out.status!==0)throw Error('artifact_read_rejected');
      body={...JSON.parse(out.stdout),session_id:session.id,acp_session_id:session.status().acp_session_id};
    }
    else if(m.method==='POST' && m.path==='/request-index'){body=session.requestIndex(m.body?.offset);}
    else if(m.method==='POST' && m.path==='/answer'){body=session.answer(m.body);}
    else if(m.method==='POST' && m.path==='/prompt'){
      if(m.body?.brief_sha256){
        if(Object.keys(m.body).some(k=>!['request_id','brief_sha256'].includes(k)) || !/^[a-f0-9]{64}$/.test(m.body.brief_sha256))throw Error('invalid_brief');
        const path='/workspace/repo/.nomad-briefs/'+m.body.brief_sha256;
        const {openSync,closeSync,readFileSync,fstatSync,constants}=await import('node:fs');
        const directory=openSync('/workspace/repo/.nomad-briefs',constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);
        const fd=openSync('/proc/self/fd/'+directory+'/'+m.body.brief_sha256,constants.O_RDONLY|constants.O_NOFOLLOW);
        try {const st=fstatSync(fd);if(!st.isFile() || st.size>policy.max_prompt_bytes)throw Error('brief_quota');const prompt=readFileSync(fd,'utf8');if(createHash('sha256').update(prompt).digest('hex')!==m.body.brief_sha256)throw Error('brief_hash');m.body={request_id:m.body.request_id,prompt};}finally{closeSync(fd);closeSync(directory);}
      }
      body=session.submit(m.body);status=202;}

    else if(m.method==='POST' && ['/cancel','/close'].includes(m.path) && Object.keys(m.body || {}).length===0){
      if(m.path==='/cancel'){await session.cancel();body={cancel_requested:true};}
      else {await session.close();body=session.status();}
    }else throw Error('unknown_operation');
    console.log(JSON.stringify({rpc_id:m.rpc_id,status,body}));
    if(m.path==='/close')finishTransport();
  }catch{if(m?.rpc_id)console.log(JSON.stringify({rpc_id:m.rpc_id,status:409,body:{error:'rejected_inspect_status_do_not_retry'}}));}
});
function finishTransport(){stopping=true;clearInterval(timer);rl.close();process.stdin.destroy();}
async function stop(reason){if(stopping)return;try{await ready;await session.close(reason);}finally{finishTransport();}}
ready.catch(()=>{process.exitCode=2;finishTransport();});
rl.once('close',()=>{if(!stopping)stop('transport_closed');});
process.once('SIGTERM',()=>stop('allocation_stop'));process.once('SIGINT',()=>stop('allocation_stop'));
