"""Legacy session HMAC/parent bindings and operator port gate; no resource budgets."""
import json,os
from pathlib import Path
CONFIG=json.loads(Path(os.environ.get('BOXLITE_BINDINGS_FILE',str(Path(__file__).with_name('legacy-bindings.json')))).read_text())
PROFILES=CONFIG['profiles']
def profile(name):
    if name not in PROFILES:raise ValueError('Unapproved profile')
    return dict(PROFILES[name])
def profile_for_parent(parent):
    for name,p in PROFILES.items():
        if p['parent']==parent:return name
    raise ValueError('Unapproved parent')
def port(value):
    value=int(value)
    if not CONFIG['ports']['min']<=value<=CONFIG['ports']['max']:raise ValueError('Unapproved allocation port')
    return value
