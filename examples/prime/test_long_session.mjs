import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {Session,selectAgentModel} from './guest/session.mjs';
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function setup(){
 let now=0,closed=0,turnArgs;const complete=deferred(),events=deferred(),sent=[];
 const root=mkdtempSync(tmpdir()+'/boxlite-session-');mkdirSync(root+'/repo');
 const runtime={ensureSession:async()=>({backendSessionId:'acp'}),startTurn(a){turnArgs=a;return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){await events.promise;}},result:complete.promise,cancel:async()=>{complete.resolve({status:'cancelled'});events.resolve();}};},close:async()=>{closed++;}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{clock:()=>now,cwd:root+'/repo',stateDir:root+'/state',onEvent:e=>sent.push(e)});
 return {s,complete,events,sent,root,advance:n=>now+=n,get closed(){return closed;},get turnArgs(){return turnArgs;}};
}
test('days idle/active are not implicit expiry; turn timeout0 and >4KiB prompt',async()=>{
 const f=setup();await f.s.initialize();f.advance(5*86400000);await f.s.tick();assert.equal(f.closed,0);
 f.s.submit({request_id:'11111111-1111-4111-8111-111111111111',prompt:'x'.repeat(80000)});await Promise.resolve();f.advance(7*86400000);await f.s.tick();assert.equal(f.closed,0);assert.equal(f.turnArgs.timeoutMs,0);
 f.complete.resolve({status:'completed'});f.events.resolve();await f.s.records.values().next().value.done;assert.equal(f.s.status().state,'ready');assert(f.sent.some(e=>e.event==='request_terminal'));
});
test('explicit configured expiry works, active turn ignores idle expiry',async()=>{
 const f=setup();f.s.idleMs=10;await f.s.initialize();f.advance(11);await f.s.tick();assert.equal(f.closed,1);
});
test('actual ACP form question -> answer, duplicate delivery safe and conflicting answer rejected',async()=>{
 const f=setup();await f.s.initialize();const ctx={requestId:'q1',signal:new AbortController().signal};
 const pending=f.s.elicit('r1',{mode:'form',message:'Choose name',requestedSchema:{type:'object',properties:{name:{type:'string'}}}},ctx);
 assert.equal(f.sent.at(-1).event,'question');const answer={question_id:'q1',answer_id:'22222222-2222-4222-8222-222222222222',action:'accept',content:{name:'test'}};
 assert.equal(f.s.answer(answer).delivered_to_runtime,true);assert.equal(f.s.answer(answer).duplicate,true);assert.deepEqual(await pending,{action:'accept',content:{name:'test'}});
 assert.throws(()=>f.s.answer({...answer,content:{name:'other'}}));const saved=JSON.parse(readFileSync(f.root+'/state/fixture.json'));assert.equal(saved.questions[0].state,'answered');assert.equal(saved.questions[0].answer.content.name,'test');
});
test('permission policy never approved by question handler; URL elicit declined; cancellation settles pending input',async()=>{
 const f=setup();assert.deepEqual(await f.s.elicit('r',{mode:'url'},{}),{action:'decline'});
 const c=new AbortController(),p=f.s.elicit('r',{mode:'form',message:'x'},{requestId:'q',signal:c.signal});c.abort();assert.deepEqual(await p,{action:'cancel'});
});

test('failed runtime result retains bounded cause without bearer or automatic retry',async()=>{
 const f=setup();await f.s.initialize();const id='33333333-3333-4333-8333-333333333333';
 f.s.submit({request_id:id,prompt:'fixture'});f.complete.resolve({status:'failed',error:{code:'provider_timeout',detailCode:'http_timeout',message:'Bearer SECRET timed out'}});f.events.resolve();await f.s.records.get(id).done;
 const r=f.s.result(id);assert.equal(r.state,'failed_no_retry');assert.equal(r.error.code,'provider_timeout');assert.equal(r.error.automatic_retry,false);assert(!r.error.message.includes('SECRET'));
});

test('execution and ACP runtime trace correlate real events without payload',async()=>{
 const out=[],completed=deferred(),released=deferred();let now=10;
 const id='44444444-4444-4444-8444-444444444444';
 const runtime={ensureSession:async()=>({backendSessionId:'55555555-5555-4555-8555-555555555555'}),
  startTurn(){return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){yield {type:'tool_call',tag:'tool_call',toolCallId:'private-command-secret-id',kind:'execute',status:'in_progress',title:'PRIVATE_COMMAND',rawInput:{command:'TOKEN_VALUE'}};now+=25;yield {type:'tool_call',tag:'tool_call_update',toolCallId:'private-command-secret-id',kind:'execute',status:'completed',text:'FILE_CONTENT'};yield {type:'text_delta',tag:'agent_message_chunk',stream:'output',text:'PRIVATE_MODEL_REPLY'};await released.promise;}},result:completed.promise,cancel:async()=>{}};},close:async()=>{}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{clock:()=>now,onEvent:e=>out.push(e)});await s.initialize();s.submit({request_id:id,prompt:'PRIVATE_USER_PROMPT'});
 await new Promise(r=>setImmediate(r));completed.resolve({status:'completed',stopReason:'end_turn'});released.resolve();await s.records.get(id).done;
 const metadata=out.filter(e=>['acp_trace','progress','request_accepted','request_running','request_terminal'].includes(e.event));
 const serialized=JSON.stringify(metadata);
 for(const text of ['PRIVATE_USER_PROMPT','PRIVATE_MODEL_REPLY','PRIVATE_COMMAND','TOKEN_VALUE','FILE_CONTENT','private-command-secret-id'])assert(!serialized.includes(text));
 assert(metadata.some(e=>e.event==='acp_trace'&&e.action==='send'&&e.observation==='prompt_written'&&e.method==='session/prompt'&&e.request_id===id));
 assert(metadata.some(e=>e.event==='acp_trace'&&e.action==='notification'&&e.method==='session/update'));
 const tools=metadata.filter(e=>e.event==='progress');assert.equal(tools.length,2);assert.equal(tools[0].tool_call_id_sha256,tools[1].tool_call_id_sha256);assert.equal(tools[1].elapsed_ms,25);
 assert(metadata.some(e=>e.event==='request_terminal'&&e.state==='completed'&&e.elapsed_ms===25));
});
test('observed timeout is logged without raw error; no synthetic send before prompt is written',async()=>{
 const events=[];const fail=Object.assign(Error('PRIVATE_ERROR Bearer SECRET'),{name:'TimeoutError',code:'ACP_REQUEST_TIMEOUT'});
 const runtime={ensureSession:async()=>({backendSessionId:'acp'}),startTurn(){return {promptStarted:Promise.reject(fail),events:{async *[Symbol.asyncIterator](){}},result:Promise.resolve({status:'failed'})};},close:async()=>{}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{onEvent:e=>events.push(e)});await s.initialize();const id='00000000-0000-4000-8000-000000000015';s.submit({request_id:id,prompt:'PRIVATE'});await s.records.get(id).done;
 const trace=events.filter(e=>e.event==='acp_trace');assert(trace.some(e=>e.action==='timeout'&&e.error_code==='ACP_REQUEST_TIMEOUT'));
 assert(!trace.some(e=>e.observation==='prompt_written'));assert(!JSON.stringify(trace).includes('PRIVATE_ERROR'));assert(!JSON.stringify(trace).includes('SECRET'));
});

test('ACP intended plan/output reaches coordinator, thoughts and unknown streams never do',async()=>{
 const out=[],done=deferred(),released=deferred();let now=0;
 const rid='66666666-6666-4666-8666-666666666666';
 const runtime={ensureSession:async()=>({backendSessionId:'fixture'}),startTurn(){return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){yield {type:'status',tag:'plan',text:'Preparing browser tests',entries:[{content:'Run local browser smoke',status:'in_progress',priority:'high',extra:'DO_NOT_FORWARD'}]};yield {type:'text_delta',tag:'agent_message_chunk',stream:'output',text:'Starting the long test'};yield {type:'text_delta',tag:'agent_thought_chunk',stream:'thought',text:'PRIVATE_THOUGHT'};yield {type:'text_delta',stream:'unknown',text:'UNKNOWN_STREAM'};await released.promise;}},result:done.promise,cancel:async()=>{}};},close:async()=>{}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{clock:()=>now,onEvent:e=>out.push(e)});await s.initialize();s.submit({request_id:rid,prompt:'PRIVATE_PROMPT'});await new Promise(r=>setImmediate(r));now=90000;
 assert.equal(s.status().running_elapsed_ms,90000);assert.equal(s.status().active_request_id,rid);
 const plan=out.find(e=>e.event==='agent_progress');assert.equal(plan.text,'Preparing browser tests');assert.equal(plan.entries[0].status,'in_progress');assert.equal(plan.plan_entry_count,1);assert(!JSON.stringify(plan).includes('DO_NOT_FORWARD'));
 const text=out.filter(e=>e.event==='assistant_delta').map(e=>e.text).join('');assert.equal(text,'Starting the long test');assert(!JSON.stringify(out).includes('PRIVATE_THOUGHT'));assert(!text.includes('UNKNOWN_STREAM'));assert(!JSON.stringify(out).includes('PRIVATE_PROMPT'));
 done.resolve({status:'completed'});released.resolve();await s.records.get(rid).done;assert.equal(s.status().running_elapsed_ms,null);
});

test('guest model policy default/explicit/unknown and status are consistent',async()=>{
 const p={provider:'operator-provider',default_model:'model-default',allowed_models:['model-default']};assert.equal(selectAgentModel(p,undefined),'model-default');assert.equal(selectAgentModel(p,'model-default'),'model-default');assert.throws(()=>selectAgentModel(p,'unapproved-model'));assert.throws(()=>selectAgentModel({...p,provider:'https://provider.example'},undefined));
 const s=new Session({ensureSession:async()=>({backendSessionId:'fixture'})},'a'.repeat(64),'fixture',{model:selectAgentModel(p,undefined)});await s.initialize();assert.equal(s.status().model,'model-default');
});
