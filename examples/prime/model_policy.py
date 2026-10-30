"""Operator-owned model choices. No provider URL or secret is accepted in a request."""
import re
def model_id(value):
 if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}',value):raise ValueError('Invalid model ID')
 return value
def select_model(request,policy):
 allowed=policy.get('allowed_models');default=policy.get('default_model');provider=policy.get('provider')
 if not isinstance(allowed,list) or not 1<=len(allowed)<=128 or not all(isinstance(x,str) for x in allowed) or len(set(allowed))!=len(allowed):raise ValueError('Operator allowed model policy required')
 for value in allowed:model_id(value)
 if default not in allowed or not isinstance(provider,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',provider):raise ValueError('Invalid operator default/provider policy')
 selected=request.get('model',default);model_id(selected)
 if selected not in allowed:raise ValueError('Requested model is not in the operator allowed model policy')
 return selected

def session_models(models,policy,session_id):
 """Prime provider headers for one durable conversation, never per-POST IDs."""
 import copy,uuid
 select_model({},policy)
 if not isinstance(session_id,str) or str(uuid.UUID(session_id))!=session_id:raise ValueError('Canonical session UUID required')
 result=copy.deepcopy(models);provider=result['providers'][policy['provider']]
 headers=provider.get('headers',{})
 if not isinstance(headers,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in headers.items()):raise ValueError('Invalid provider headers')
 # HTTP names are case-insensitive. Never let a stale configured session win.
 provider['headers']={k:v for k,v in headers.items() if k.lower()!='x-opencode-session'}
 provider['headers']['x-opencode-session']=session_id
 return result
