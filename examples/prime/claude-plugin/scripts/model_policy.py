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
