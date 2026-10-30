"""Strict payload-free execution/ACP metadata for logs and activity views."""
import hashlib,math,time,uuid
EVENTS={'agent_progress','request_accepted','request_running','request_terminal','progress','question','answer_recorded','answer_delivered','acp_trace'}
STATES={'accepted','running','completed','cancelled','failed','failed_no_retry','uncertain_no_retry','pending','in_progress','unknown','starting','exited','cancel_requested','answered','ready','busy'}
KINDS={'read','edit','delete','move','search','execute','fetch','think','switch_mode','other'}
ERRORS={'ACP_BACKEND_MISSING','ACP_BACKEND_UNAVAILABLE','ACP_BACKEND_UNSUPPORTED_CONTROL','ACP_DISPATCH_DISABLED','ACP_INVALID_RUNTIME_OPTION','ACP_SESSION_INIT_FAILED','ACP_TURN_FAILED','ACP_REQUEST_TIMEOUT','ETIMEDOUT','TIMEOUT','provider_timeout','http_timeout','kernel_startup_failed','kernel_missing_default_packages','runtime_error'}
TYPES={'Error','RuntimeError','TimeoutError','TypeError','RangeError','AbortError','AcpRuntimeError'}
OPERATIONS={'ensureSession','startTurn','turn.events','turn.result','turn.cancel','agent_process'}
OBSERVATIONS={'runtime_api','prompt_written','runtime_turn_result','normalized_event','process_lifecycle'}
UPDATES={'tool_call','tool_call_update','agent_message_chunk','agent_thought_chunk','usage_update','available_commands_update','current_mode_update','config_option_update','session_info_update','plan','text_delta','status','client_operation','other'}
def identifier(value):
 if value is None:return None
 try:return str(uuid.UUID(value))
 except (ValueError,TypeError,AttributeError):return 'opaque:'+hashlib.sha256(str(value).encode()).hexdigest()[:16]
def member(value,allowed):return isinstance(value,str) and value in allowed
def number(value):return isinstance(value,(float,int)) and not isinstance(value,bool) and 0<=value<=2**53 and math.isfinite(value)

CHUNK_UPDATES={'agent_thought_chunk','agent_message_chunk','text_delta'}
DUPLICATE_TOOL_UPDATES={'tool_call','tool_call_update'}
def logging_policy(path,read_protected):
 try:policy=read_protected(path)
 except FileNotFoundError:return {'chunk_metadata':False}
 if not isinstance(policy,dict) or set(policy)!={'chunk_metadata'} or type(policy['chunk_metadata']) is not bool:
  raise ValueError('Logging configuration requires boolean chunk_metadata only')
 return policy

def plain_output_metadata(stream,text,secrets_ready):
 if secrets_ready:
  return {'event':'runtime_stderr' if stream=='stderr' else 'runtime_stdout','layer':'companion_runtime','stage':'runtime_output','stream':stream}
 return {'event':'secret_bootstrap_log','layer':'secret_provider','stage':'envconsul_start','stream':stream,'error_classes':[k for k in ['permission denied','certificate','timeout','no route to host','connection refused'] if k in text.lower()]}

def project(event,context=None,now=None,chunk_metadata=False):
 name=event.get('event')
 if not member(name,EVENTS):return None
 if type(chunk_metadata) is not bool:raise ValueError('chunk_metadata must be boolean')
 if name=='acp_trace' and event.get('action')=='notification' and isinstance(event.get('update_type'),str) and event['update_type'] in CHUNK_UPDATES|DUPLICATE_TOOL_UPDATES and not chunk_metadata:return None
 out={'event':name,'layer':'acp_runtime' if name=='acp_trace' else 'prime_execution','timestamp':time.time() if now is None else now}
 for key in ['allocation_id','request_id','session_id','acp_session_id']:
  value=(context or {}).get(key) if key in ['allocation_id','session_id'] and context else event.get(key)
  if value is not None:out[key]=identifier(value)
 if context and isinstance(context.get('vm_id'),str):out['vm_id']=context['vm_id']
 if number(event.get('timestamp')):out['guest_timestamp']=event['timestamp']
 if member(event.get('state'),STATES):out['state']=event['state']
 if member(event.get('status'),STATES):out['status']=event['status']
 if name=='request_terminal':out['task_result']=task_result(event)
 if member(event.get('kind'),KINDS):out['tool_name']=event['kind']
 tool=event.get('tool_call_id_sha256')
 if isinstance(tool,str) and len(tool)==16 and all(x in '0123456789abcdef' for x in tool):out['tool_call_id_sha256']=tool
 for key in ['question_id','answer_id']:
  if key in event:out[key]=identifier(event[key])
 if number(event.get('elapsed_ms')):out['elapsed_ms']=round(event['elapsed_ms'],3)
 if member(event.get('error_code'),ERRORS):out['error_code']=event['error_code']
 error=event.get('error')
 if error:
  out['error_present']=True
  out['error_code']=next((v for v in [error.get('code'),error.get('detailCode')] if member(v,ERRORS)),'runtime_error') if isinstance(error,dict) else 'runtime_error'
 if name=='agent_progress':
  if event.get('message_kind')!='plan':return None
  out['message_kind']='plan'
  if type(event.get('plan_entry_count')) is int and 0<=event['plan_entry_count']<=64:out['plan_entry_count']=event['plan_entry_count']
 if name=='acp_trace':
  if not member(event.get('action'),{'send','receive','notification','error','timeout','disconnect'}):return None
  out['action']=event['action'];out['rpc_id_available']=False
  for key,allowed in [('operation',OPERATIONS),('observation',OBSERVATIONS),('update_type',UPDATES),('error_type',TYPES),('error_kind',{'timeout','backend_unavailable','runtime_error'})]:
   if member(event.get(key),allowed):out[key]=event[key]
  if member(event.get('method'),{'session/prompt','session/update'}):out['method']=event['method']
  if member(event.get('signal'),{'SIGTERM','SIGKILL','SIGINT','SIGABRT','SIGSEGV','SIGHUP','SIGPIPE'}):out['signal']=event['signal']
  for key in ['exit_code','pid']:
   value=event.get(key)
   if isinstance(value,int) and not isinstance(value,bool) and abs(value)<2**31:out[key]=value
 return out

def terminal_metadata(value):
 """Categorical public projection; guest-supplied nested metadata is untrusted."""
 if not isinstance(value,dict):return None
 result={'task_completion':'unverified','automatic_retry':False}
 for key,allowed in [('observation',{'terminal','error','cancelled','incomplete','unknown'}),('stop_reason',{'end_turn','max_tokens','max_turn_requests','refusal','cancelled','length','stop','tool_calls'}),('error_code',{'upstream_host_error','upstream_protocol_error','upstream_stream_idle_timeout','ACP_REQUEST_TIMEOUT','ETIMEDOUT','provider_timeout','http_timeout','EOF','ECONNRESET'})]:
  result[key]=value.get(key) if isinstance(value.get(key),str) and value[key] in allowed else None
 for key,low,high in [('http_status',100,599),('attempt_count',0,100)]:
  v=value.get(key);result[key]=v if type(v) is int and low<=v<=high else None
 digest=value.get('upstream_request_id_sha256');result['upstream_request_id_sha256']=digest if isinstance(digest,str) and __import__('re').fullmatch('[a-f0-9]{64}',digest) else None
 return result


def task_result(value):
 """Derive task outcome from categorical turn metadata, never from text or tools."""
 state=value.get('state');meta=value.get('terminal_metadata');meta=meta if isinstance(meta,dict) else {}
 reason=value.get('stop_reason') or meta.get('stop_reason')
 limited=state=='completed' and member(reason,{'max_tokens','length','max_turn_requests'})
 outcome='incomplete' if limited else 'cancelled' if state=='cancelled' else 'failed' if member(state,{'failed','failed_no_retry'}) else 'unverified' if state=='completed' else 'running' if member(state,{'running','accepted','pending'}) else 'unknown'
 code=('turn_request_limit' if reason=='max_turn_requests' else 'output_token_limit') if limited else 'turn_ended_task_unverified' if outcome=='unverified' else 'task_outcome_unknown' if outcome=='unknown' else outcome
 return {'state':outcome,'limited':limited,'reason_code':code,'completion_verified':False,'automatic_retry':False}

def result_view(value):
 """Add explicit outcome for live or legacy saved results without rewriting ACP state."""
 outcome=task_result(value);status=value.get('acp_status')
 if not member(status,{'pending','in_progress','completed','failed','cancelled','incomplete'}):status=value.get('state') if member(value.get('state'),{'completed','cancelled'}) else 'unknown'
 out={**value,'acp_status':status,'task_result':outcome}
 if outcome['limited']:out['phase']='limited'
 elif out.get('phase')=='limited':out.pop('phase')
 return out
