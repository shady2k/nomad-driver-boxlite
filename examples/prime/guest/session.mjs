// One in-process acpx runtime and one live handle. No reconnection/recreation.
import {mkdirSync,writeFileSync,renameSync,readFileSync,existsSync} from 'node:fs';
import {randomUUID,createHash} from 'node:crypto';
import { timingSafeEqual } from 'node:crypto';
export function runtimeErrorMetadata(e) {
  const known=['ACP_BACKEND_MISSING','ACP_BACKEND_UNAVAILABLE','ACP_BACKEND_UNSUPPORTED_CONTROL','ACP_DISPATCH_DISABLED','ACP_INVALID_RUNTIME_OPTION','ACP_SESSION_INIT_FAILED','ACP_TURN_FAILED','ACP_REQUEST_TIMEOUT','ETIMEDOUT','TIMEOUT','provider_timeout','http_timeout','kernel_startup_failed','kernel_missing_default_packages'];
  const code=known.includes(e?.code)?e.code:known.includes(e?.detailCode)?e.detailCode:'runtime_error';
  const timeout=['ACP_REQUEST_TIMEOUT','ETIMEDOUT','TIMEOUT','provider_timeout','http_timeout'].includes(code)||e?.name==='TimeoutError';
  return {error_code:code,error_type:['Error','RuntimeError','TimeoutError','TypeError','RangeError','AbortError','AcpRuntimeError'].includes(e?.name)?e.name:'Error',
    error_kind:timeout?'timeout':code==='ACP_BACKEND_UNAVAILABLE'?'backend_unavailable':'runtime_error'};
}
// Structured runtime metadata only. Never infer task success from an ACP turn ending.
export function terminalMetadata(result={}) {
  const error=result.error??{};
  const codes=['upstream_host_error','upstream_protocol_error','upstream_stream_idle_timeout','ACP_REQUEST_TIMEOUT','ETIMEDOUT','provider_timeout','http_timeout','EOF','ECONNRESET'];
  const code=codes.includes(error.code)?error.code:codes.includes(error.detailCode)?error.detailCode:null;
  const reason=['end_turn','max_tokens','max_turn_requests','refusal','cancelled','length','stop','tool_calls'].includes(result.stopReason)?result.stopReason:null;
  const http=error.httpStatus??result.httpStatus;
  const attempts=result.attemptCount;
  const observation=result.status==='failed'?'error':result.status==='cancelled'?'cancelled':result.status==='incomplete'?'incomplete':result.status==='completed'&&reason?'terminal':'unknown';
  return {observation,stop_reason:reason,error_code:code,
    http_status:Number.isInteger(http)&&http>=100&&http<=599?http:null,
    attempt_count:Number.isInteger(attempts)&&attempts>=0&&attempts<=100?attempts:null,
    upstream_request_id_sha256:typeof result.upstreamRequestId==='string'&&result.upstreamRequestId.length<=512?createHash('sha256').update(result.upstreamRequestId).digest('hex'):null,
    task_completion:'unverified',automatic_retry:false};
}
// A terminal ACP turn is not proof the user's task was completed.
export function taskResult(record={}) {
  const reason=record.stop_reason??record.terminal_metadata?.stop_reason;
  const limited=record.state==='completed'&&['max_tokens','length','max_turn_requests'].includes(reason);
  const state=limited?'incomplete':record.state==='cancelled'?'cancelled':record.state==='failed_no_retry'||record.state==='failed'?'failed':record.state==='completed'?'unverified':['running','accepted','pending'].includes(record.state)?'running':'unknown';
  return {state,limited,reason_code:limited?(reason==='max_turn_requests'?'turn_request_limit':'output_token_limit'):state==='unverified'?'turn_ended_task_unverified':state==='unknown'?'task_outcome_unknown':state,completion_verified:false,automatic_retry:false};
}
export function selectAgentModel(policy,inputModel) {
  const valid=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/.test(value);
  if(!policy||typeof policy.provider!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(policy.provider)||!Array.isArray(policy.allowed_models)||!policy.allowed_models.length||policy.allowed_models.length>128||!policy.allowed_models.every(valid)||new Set(policy.allowed_models).size!==policy.allowed_models.length||!policy.allowed_models.includes(policy.default_model))throw Error('Operator model policy required');
  const model=inputModel??policy.default_model;
  if(!valid(model)||!policy.allowed_models.includes(model))throw Error('Requested model is not allowed by operator policy');
  return model;
}
export class Session {
  constructor(runtime, token, id, { model = null, clock = () => performance.now(), idleMs = 0, maxMs = 0, turnTimeoutMs = 0, maxPromptBytes=1048576,maxResultBytes=8388608,onEvent=()=>{}, stateDir=null,cwd = '/workspace/repo' } = {}) {
    if (!/^[a-f0-9]{64}$/.test(token)) throw Error('invalid credential');
    this.runtime=runtime; this.token=token; this.id=id;this.model=model; this.clock=clock;
    this.idleMs=idleMs; this.maxMs=maxMs; this.born=clock(); this.last=this.born;
    this.cwd=cwd; this.handle=null; this.turn=null; this.closed=false; this.reason=null;
    this.records=new Map(); this.closing=null;this.questions=new Map();this.activeRequestId=null;this.onEvent=onEvent;this.turnTimeoutMs=turnTimeoutMs;this.maxPromptBytes=maxPromptBytes;this.maxResultBytes=maxResultBytes;this.stateDir=stateDir;
  }
  trace(action,details={}) {
    this.onEvent({event:'acp_trace',layer:'acp_runtime',timestamp:Date.now()/1000,
      session_id:this.id,acp_session_id:this.handle?.backendSessionId ?? this.handle?.agentSessionId,
      action,...details,rpc_id_available:false});
  }
  traceError(error,details={}) {
    const meta=runtimeErrorMetadata(error);
    this.trace(meta.error_kind==='timeout'?'timeout':'error',{...details,...meta});
  }
  traceNotification(e,requestId) {
    const known=['tool_call','tool_call_update','agent_message_chunk','agent_thought_chunk','usage_update','available_commands_update','current_mode_update','config_option_update','session_info_update','plan'];
    const tag=known.includes(e.tag)?e.tag:null;
    this.trace('notification',{operation:'turn.events',observation:'normalized_event',method:tag?'session/update':null,
      update_type:tag ?? (['tool_call','text_delta','status','plan','usage_update','client_operation'].includes(e.type)?e.type:'other'),
      request_id:requestId,...(typeof e.toolCallId==='string'?{tool_call_id_sha256:createHash('sha256').update(e.toolCallId).digest('hex').slice(0,16)}:{})});
  }
  authorized(header) {
    const a=Buffer.from(header || ''); const b=Buffer.from('Bearer '+this.token);
    return a.length===b.length && timingSafeEqual(a,b);
  }
  async initialize() {
    if(this.handle || this.closed) throw Error('already initialized');
    this.trace('send',{operation:'ensureSession',observation:'runtime_api'});
    try {
      this.handle=await this.runtime.ensureSession({sessionKey:this.id, agent:'prime', mode:'persistent',cwd:this.cwd, sessionOptions:{allowedTools:[]}});
      this.trace('receive',{operation:'ensureSession',observation:'runtime_api',status:'completed'});
    } catch(e) {this.traceError(e,{operation:'ensureSession',observation:'runtime_api'});throw e;}
  }
  status() { return {model:this.model,protocol_version:2,capabilities:{execution_metadata:true,acp_runtime_trace:true,event_stream:true,chunked_artifacts:true,workspace_upload:true,form_elicitation_handler:true,prime_form_elicitation_emission:false,mid_turn_steer:false},session_id:this.id, acp_session_id:this.handle?.backendSessionId ?? this.handle?.agentSessionId,
    backend_session_id:this.handle?.backendSessionId, record_id:this.handle?.acpxRecordId,
    state:this.closed?'closed':this.turn?'busy':'ready',reason:this.reason,active_request_id:this.activeRequestId,running_elapsed_ms:this.activeRequestId&&this.records.has(this.activeRequestId)?Math.max(0,Math.round(this.clock()-this.records.get(this.activeRequestId).requestedAt)):null,
    questions:[...this.questions.values()].filter(q=>q.state==='pending').map(({id,request_id,message,schema,state})=>({question_id:id,request_id,message,schema,state})),
    request_count:this.records.size,requests_truncated:this.records.size>64,requests:[...this.records.values()].slice(-64).map(({request_id,state})=>({request_id,state}))}; }
  async tick() {
    if(this.closed)return;
    if(this.maxMs>0 && this.clock()-this.born>=this.maxMs) await this.close('absolute_timeout');
    else if(this.idleMs>0 && !this.turn && this.clock()-this.last>=this.idleMs)await this.close('idle_timeout');
  }
  submit(d) {
    if(this.closed || !this.handle)throw Error('closed_or_not_ready');
    if(!d || Object.keys(d).some(k=>!['request_id','prompt'].includes(k)) ||
       !/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(d.request_id || '') ||
       typeof d.prompt!=='string' || !d.prompt.trim() || Buffer.byteLength(d.prompt)>this.maxPromptBytes)throw Error('invalid_request');
    // No queue and no automatic replay after an ambiguous client disconnect.
    if(this.records.has(d.request_id))throw Error('duplicate_request');
    if(this.turn)throw Error('busy');
    if(this.records.size>=4096)throw Error('session_request_limit');
    const record={request_id:d.request_id,state:'accepted',requestedAt:this.clock(),text:'',tools:new Map(),tools_truncated:false,truncated:false};
    this.records.set(d.request_id,record);this.activeRequestId=d.request_id; this.last=this.clock();this.publish({event:'request_accepted',request_id:d.request_id,state:'accepted'});
    try {
      this.turn=this.runtime.startTurn({handle:this.handle, mode:'prompt',requestId:d.request_id,
        text:d.prompt,timeoutMs:this.turnTimeoutMs,onElicitation:(q,c)=>this.elicit(d.request_id,q,c)});
    } catch(e) {record.state='failed_no_retry';record.error=this.safeError(e);this.last=this.clock();this.activeRequestId=null;this.traceError(e,{operation:'startTurn',observation:'runtime_api',request_id:d.request_id});this.publish({event:'request_terminal',request_id:d.request_id,state:record.state,error:record.error,terminal_metadata:record.terminal_metadata,task_result:taskResult(record),elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});throw Error('start_uncertain');}
    const turn=this.turn;
    record.done=(async()=>{
      try {
        await turn.promptStarted;
        this.trace('send',{operation:'startTurn',method:'session/prompt',observation:'prompt_written',request_id:d.request_id});
        record.state='running';this.publish({event:'request_running',request_id:d.request_id,state:record.state});
        for await(const e of turn.events) {
          this.traceNotification(e,d.request_id);
          // Only allowlisted tool metadata; never args, raw output or thoughts.
          if(e.type==='tool_call' && typeof e.toolCallId==='string' && e.toolCallId.length<=128) {
            if(record.tools.has(e.toolCallId) || record.tools.size<256) {
              const previous=record.tools.get(e.toolCallId) || {kind:'other',status:'unknown',startedAt:this.clock()};
              const kind=['read','edit','delete','move','search','execute','fetch','think','switch_mode','other'].includes(e.kind)?e.kind:previous.kind;
              const status=['pending','in_progress','completed','failed'].includes(e.status)?e.status:previous.status;
              const hint=/missing default Python packages/.test(String(e.text || ''))?'kernel_missing_default_packages':/kernel startup failed/.test(String(e.text || ''))?'kernel_startup_failed':previous.hint;
              record.tools.set(e.toolCallId,{kind,status,hint,startedAt:previous.startedAt});this.publish({event:'progress',request_id:d.request_id,kind,status,tool_call_id_sha256:createHash('sha256').update(e.toolCallId).digest('hex').slice(0,16),elapsed_ms:Math.max(0,Math.round(this.clock()-previous.startedAt)),...(hint?{error_code:hint}:{})});
            } else record.tools_truncated=true;
          }
          // ACP plan is intended coordinator-facing content, never infrastructure stdout.
          if(e.tag==='plan' && ['status','plan'].includes(e.type)) {
            const clean=value=>String(value??'').split(this.token).join('[REDACTED]');
            const entries=Array.isArray(e.entries)?e.entries.slice(0,64).filter(x=>x&&typeof x.content==='string').map(x=>({content:clean(x.content).slice(0,512),status:['pending','in_progress','completed'].includes(x.status)?x.status:'pending',priority:['high','medium','low'].includes(x.priority)?x.priority:'medium'})):[];
            this.publish({event:'agent_progress',message_kind:'plan',request_id:d.request_id,text:clean(e.text).slice(0,4096),entries,plan_entry_count:entries.length,elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});
          }
          // Expose assistant output only, not thoughts or arbitrary raw ACP data.
          if(e.type==='text_delta' && e.stream==='output' && e.tag!=='agent_thought_chunk') {
            const text=String(e.text || '').split(this.token).join('[REDACTED]');
            if(Buffer.byteLength(record.text+text)<=this.maxResultBytes){record.text+=text;for(let start=0;start<text.length;start+=4096)this.onEvent({event:'assistant_delta',request_id:d.request_id,session_id:this.id,text:text.slice(start,start+4096)});}
            else record.truncated=true;
          }
        }
        const r=await turn.result;
        record.acp_status=['pending','in_progress','completed','failed','cancelled','incomplete'].includes(r.status)?r.status:'unknown';
        record.terminal_metadata=terminalMetadata(r);
        if(r.status==='failed')this.traceError(r.error,{operation:'turn.result',method:'session/prompt',observation:'runtime_turn_result',request_id:d.request_id,status:'failed',elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});
        else this.trace('receive',{operation:'turn.result',method:'session/prompt',observation:'runtime_turn_result',request_id:d.request_id,status:['completed','cancelled'].includes(r.status)?r.status:'unknown',elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});
        record.state=['completed','cancelled'].includes(r.status)?r.status:'failed_no_retry';
        record.stop_reason=r.stopReason;
        if(r.status==='failed')record.error=this.safeError(r.error);
      } catch(e) {record.state='uncertain_no_retry';record.error=this.safeError(e);record.terminal_metadata=terminalMetadata({status:'incomplete',error:e});this.traceError(e,{operation:'startTurn',method:'session/prompt',observation:'runtime_api',request_id:d.request_id,elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});}
      finally {this.turn=null;this.activeRequestId=null;this.last=this.clock();this.publish({event:'request_terminal',request_id:d.request_id,state:record.state,error:record.error,terminal_metadata:record.terminal_metadata,task_result:taskResult(record),elapsed_ms:Math.max(0,Math.round(this.clock()-record.requestedAt))});}
    })();
    return {request_id:d.request_id,state:'accepted',session_id:this.id};
  }
  publish(e) {
    if(this.stateDir){
      mkdirSync(this.stateDir,{recursive:true,mode:0o700});
      const data={session_id:this.id,acp_session_id:this.handle?.backendSessionId,records:[...this.records.values()].map(({done,tools,...r})=>({...r,tools:[...tools.entries()]})),questions:[...this.questions.values()].map(({resolve,...q})=>q)};
      const path=this.stateDir+'/'+this.id+'.json',tmp=path+'.tmp';writeFileSync(tmp,JSON.stringify(data),{mode:0o600});renameSync(tmp,path);
    }
    if(e.event==='request_terminal' && this.stateDir){
      const dir=this.cwd+'/.nomad-results';mkdirSync(dir,{recursive:true,mode:0o700});
      writeFileSync(dir+'/'+e.request_id+'.json',JSON.stringify({...this.result(e.request_id),text:this.records.get(e.request_id).text,truncated:this.records.get(e.request_id).truncated}),{mode:0o600});
      const record=this.records.get(e.request_id);record.summary_only=Buffer.byteLength(record.text)>32768;record.text=Buffer.from(record.text).subarray(0,32768).toString('utf8');
    }
    this.onEvent({...e,timestamp:Date.now()/1000,session_id:this.id,acp_session_id:this.handle?.backendSessionId ?? this.handle?.agentSessionId});
  }
  async elicit(requestId,q,ctx) {
    // ACP form input only, never permission approval or URL execution.
    if(q.mode && q.mode!=='form')return {action:'decline'};
    if(this.questions.size>=4096 || [...this.questions.values()].filter(q=>q.state==='pending').length>=2 || Buffer.byteLength(JSON.stringify(q))>32768)throw Error('elicitation_quota');
    const id=String(ctx.requestId), entry={id,request_id:requestId,message:String(q.message||'').slice(0,16384),schema:q.requestedSchema,state:'pending'};
    if(this.questions.has(id))throw Error('duplicate_question');
    const response=new Promise(resolve=>{entry.resolve=resolve;});this.questions.set(id,entry);
    const abort=()=>{if(entry.state==='pending'){entry.state='cancelled';entry.resolve({action:'cancel'});}};
    ctx.signal?.addEventListener('abort',abort,{once:true});
    this.publish({event:'question',question_id:id,request_id:requestId,message:entry.message,schema:entry.schema,state:'pending'});
    try{return await response;}finally{ctx.signal?.removeEventListener('abort',abort);}
  }
  answer(d) {
    if(!d || Buffer.byteLength(JSON.stringify(d))>32768 || Object.keys(d).some(k=>!['question_id','answer_id','action','content'].includes(k)) || !['accept','decline','cancel'].includes(d.action) || !/^[0-9a-f-]{36}$/.test(d.answer_id||''))throw Error('invalid_answer');
    const q=this.questions.get(String(d.question_id));if(!q)throw Error('unknown_question');
    const digest=createHash('sha256').update(JSON.stringify(d)).digest('hex');
    if(q.state!=='pending'){if(q.answer_id===d.answer_id && q.digest===digest)return {delivered_to_runtime:true,agent_consumption_confirmed:false,duplicate:true,question_id:q.id};throw Error('question_already_answered');}
    q.answer_id=d.answer_id;q.digest=digest;q.answer={action:d.action,content:d.content};q.state='answered';this.publish({event:'answer_recorded',question_id:q.id,answer_id:d.answer_id,request_id:q.request_id});q.resolve({action:d.action,...(d.action==='accept'?{content:d.content||{}}:{})});
    this.publish({event:'answer_delivered',question_id:q.id,answer_id:d.answer_id,request_id:q.request_id});return {delivered_to_runtime:true,agent_consumption_confirmed:false,question_id:q.id,answer_id:d.answer_id};
  }
  safeError(e) {
    const message=String(e?.message||'Runtime failure without message').split(this.token).join('[REDACTED]').replace(/Bearer\s+[^\s"']+/gi,'Bearer [REDACTED]').slice(0,2048);
    return {message,...(typeof e?.code==='string'?{code:e.code.slice(0,80)}:{}),...(typeof e?.detailCode==='string'?{detailCode:e.detailCode.slice(0,80)}:{}),automatic_retry:false};
  }
  requestIndex(offset=0) {
    if(!Number.isSafeInteger(offset)||offset<0)throw Error('invalid_history_offset');
    const all=[...this.records.values()];return {requests:all.slice(offset,offset+64).map(({request_id,state})=>({request_id,state})),next_offset:offset+64<all.length?offset+64:null};
  }
  result(id) {
    const r=this.records.get(id); if(!r)throw Error('unknown_request');
    const tools=[...r.tools.values()], kinds={};
    for(const tool of tools)kinds[tool.kind]=(kinds[tool.kind] || 0)+1;
    const outcome=taskResult(r);
    return {request_id:r.request_id,state:r.state,acp_status:r.acp_status??(['completed','cancelled'].includes(r.state)?r.state:'unknown'),task_result:outcome,...(outcome.limited?{phase:'limited'}:{}),text:Buffer.from(r.text).subarray(0,32768).toString('utf8'),truncated:r.truncated||r.summary_only||Buffer.byteLength(r.text)>32768,result_file:'.nomad-results/'+r.request_id+'.json',
      tool_summary:{total:tools.length,completed:tools.filter(t=>t.status==='completed').length,
        failed:tools.filter(t=>t.status==='failed').length,kinds,failure_hints:[...new Set(tools.filter(t=>t.status==='failed' && t.hint).map(t=>t.hint))],truncated:r.tools_truncated},
      stop_reason:r.stop_reason,terminal_metadata:r.terminal_metadata??{observation:'unknown',task_completion:'unverified',automatic_retry:false},error:r.error,session_id:this.id,acp_session_id:this.handle?.backendSessionId ?? this.handle?.agentSessionId};
  }
  async cancel() {if(this.turn){const id=this.activeRequestId;this.trace('send',{operation:'turn.cancel',observation:'runtime_api',request_id:id});try{await this.turn.cancel({reason:'owner_cancel'});this.trace('receive',{operation:'turn.cancel',observation:'runtime_api',request_id:id,status:'cancel_requested'});}catch(e){this.traceError(e,{operation:'turn.cancel',observation:'runtime_api',request_id:id});throw e;}}}
  async close(reason='owner_close') {
    if(this.closing)return this.closing;
    this.closed=true;this.reason=reason;
    this.closing=(async()=>{
      try { await this.cancel(); }
      finally {
        try { if(this.handle)await this.runtime.close({handle:this.handle,reason,discardPersistentState:false}); }
        finally { await this.runtime.shutdown?.(); }
      }
    })();
    return this.closing;
  }
}
