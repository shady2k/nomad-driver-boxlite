import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Session,terminalMetadata} from './guest/session.mjs';
for(const [name,error,http] of [['503','upstream_host_error',503],['502','upstream_protocol_error',502],['stream idle','upstream_stream_idle_timeout',null]])test(name+' structured metadata without replay',()=>{
 const m=terminalMetadata({status:'failed',error:{code:error,httpStatus:http,message:'Bearer SECRET PRIVATE'},attemptCount:2,upstreamRequestId:'private-request-id'});
 assert.equal(m.observation,'error');assert.equal(m.http_status,http);assert.equal(m.error_code,error);assert.equal(m.attempt_count,2);assert.equal(m.automatic_retry,false);assert.equal(m.task_completion,'unverified');assert(!JSON.stringify(m).includes('PRIVATE'));assert(!JSON.stringify(m).includes('private-request-id'));
});
for(const tool of [false,true])test('EOF after '+(tool?'tool':'text')+' retains effects/partial text and never succeeds',async()=>{
 let calls=0;const events=[];
 const runtime={ensureSession:async()=>({backendSessionId:'fixture'}),startTurn(){calls++;return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){yield {type:'text_delta',stream:'output',text:'partial'};if(tool)yield {type:'tool_call',toolCallId:'t',kind:'execute',status:'completed'};throw Object.assign(Error('PRIVATE'),{code:'EOF'});}},result:Promise.resolve({status:'completed',stopReason:'end_turn'})};}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{onEvent:e=>events.push(e)});await s.initialize();const id='11111111-1111-4111-8111-111111111111';s.submit({request_id:id,prompt:'PRIVATE'});await s.records.get(id).done;const r=s.result(id);
 assert.equal(calls,1);assert.equal(r.state,'uncertain_no_retry');assert.equal(r.text,'partial');assert.equal(r.terminal_metadata.observation,'incomplete');assert.equal(r.task_result.state,'unknown');assert.equal(r.task_result.limited,false);assert.equal(r.acp_status,'unknown');assert.equal(r.terminal_metadata.error_code,'EOF');assert.equal(r.tool_summary.completed,tool?1:0);
});
test('end_turn is turn terminal, never task verification; unknown/limit stay distinct',()=>{
 assert.equal(terminalMetadata({status:'completed',stopReason:'end_turn'}).observation,'terminal');assert.equal(terminalMetadata({status:'completed',stopReason:'end_turn'}).task_completion,'unverified');
 assert.equal(terminalMetadata({status:'completed'}).observation,'unknown');assert.equal(terminalMetadata({status:'completed',stopReason:'max_tokens'}).stop_reason,'max_tokens');assert.equal(terminalMetadata({status:'incomplete'}).observation,'incomplete');
});
for(const status of ['failed','incomplete'])test('partial output + '+status+' remains available without task success',async()=>{
 let calls=0;const runtime={ensureSession:async()=>({backendSessionId:'fixture'}),startTurn(){calls++;return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){yield {type:'text_delta',stream:'output',text:'partial'};}},result:Promise.resolve({status,error:{code:'upstream_protocol_error',httpStatus:502}})};}};
 const s=new Session(runtime,'a'.repeat(64),'fixture');await s.initialize();const id='22222222-2222-4222-8222-222222222222';s.submit({request_id:id,prompt:'fixture'});await s.records.get(id).done;const r=s.result(id);assert.equal(calls,1);assert.equal(r.text,'partial');assert.equal(r.state,'failed_no_retry');assert.equal(r.terminal_metadata.observation,status==='failed'?'error':'incomplete');assert.equal(r.task_result.state,'failed');assert.equal(r.acp_status,status);assert.equal(r.terminal_metadata.task_completion,'unverified');
});

for(const reason of ['max_tokens','length','max_turn_requests','end_turn'])test(reason+' separates task outcome from ACP status and retains partial effects',async()=>{
 let calls=0;const emitted=[];
 const runtime={ensureSession:async()=>({backendSessionId:'fixture'}),startTurn(){calls++;return {promptStarted:Promise.resolve(),events:{async *[Symbol.asyncIterator](){yield {type:'text_delta',stream:'output',text:'partial'};yield {type:'tool_call',toolCallId:'t',kind:'execute',status:'completed'};}},result:Promise.resolve({status:'completed',stopReason:reason})};}};
 const s=new Session(runtime,'a'.repeat(64),'fixture',{onEvent:e=>emitted.push(e)});await s.initialize();const id='33333333-3333-4333-8333-333333333333';s.submit({request_id:id,prompt:'fixture'});await s.records.get(id).done;const r=s.result(id),limited=reason!=='end_turn';
 assert.equal(calls,1);assert.equal(r.state,'completed');assert.equal(r.acp_status,'completed');assert.equal(r.stop_reason,reason);assert.equal(r.text,'partial');assert.equal(r.tool_summary.completed,1);assert.equal(r.task_result.state,limited?'incomplete':'unverified');assert.equal(r.task_result.limited,limited);assert.equal(r.task_result.completion_verified,false);assert.equal(r.task_result.automatic_retry,false);assert.equal(r.phase,limited?'limited':undefined);assert.deepEqual(emitted.find(e=>e.event==='request_terminal').task_result,r.task_result);
});
