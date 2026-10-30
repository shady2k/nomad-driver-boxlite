"""Nonsecret persistent session start contract shared by the coordinator helper and Mac runner."""
import json
from pathlib import PurePosixPath
import re
import uuid
from urllib.parse import urlsplit
from pathlib import Path
import importlib.util as _iu, sys as _sys
_ps=_iu.spec_from_file_location('profiles',Path(__file__).with_name('profiles.py'))
_pm=_iu.module_from_spec(_ps);_sys.modules['profiles']=_pm;_ps.loader.exec_module(_pm)
from profiles import profile, PROFILES

PROFILE = 'legacy-small'
NAMESPACE = 'boxlite-prime'
PARENT = 'legacy-worker'


def child_job(value):
    import universal
    if universal.is_parent(value):return value
    if "/dispatch-" in value and universal.is_parent(value.split("/dispatch-",1)[0]) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}",value.split("/dispatch-",1)[1]):return value
    if not re.fullmatch(r'(?:'+ '|'.join(re.escape(p['parent']) for p in PROFILES.values()) +r')/dispatch-[A-Za-z0-9_-]{1,160}', value):
        raise ValueError('Expected a legacy-worker dispatched child ID')
    return value


def relative_file(value):
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or '\\' in value or '\0' in value
            or any(s in ('', '.', '..', '.git') for s in value.split('/'))):
        raise ValueError('Only safe relative guest file paths are allowed')
    return value


def validate_initialization(data, require_profile=False):
    if not isinstance(data, dict) or set(data) - {
            'profile', 'request_id', 'session_id', 'prompt', 'context', 'repo', 'files', 'commands'}:
        raise ValueError('Unknown request fields')
    d = dict(data)
    if require_profile:
        if not d.get('profile'):raise ValueError('Explicit legacy profile binding required')
        profile(d['profile'])
    for key in ['request_id', 'session_id']:
        d[key] = str(uuid.UUID(d[key]))
    for key, maximum in [('prompt', 4096), ('context', 4096)]:
        value = d.get(key, '')
        if not isinstance(value, str) or len(value.encode()) > maximum or '\0' in value:
            raise ValueError('Invalid or oversized prompt/context')
        d[key] = value
    if d['prompt'] or d['context']:
        raise ValueError('Start only initializes a VM; send prompts through the live endpoint')
    files = d.get('files', {})
    if not isinstance(files, dict) or len(files) > 16:
        raise ValueError('At most 16 guest text files')
    total = 0
    for name, content in files.items():
        relative_file(name)
        if not isinstance(content, str) or '\0' in content:
            raise ValueError('Guest files must be text')
        total += len(content.encode())
    if total > 8192:
        raise ValueError('Guest files exceed 8192 bytes')
    d['files'] = files
    commands = d.get('commands', [])
    if not isinstance(commands, list) or len(commands) > 4:
        raise ValueError('At most four guest-only argv commands')
    for cmd in commands:
        if (not isinstance(cmd, list) or not 1 <= len(cmd) <= 24
                or any(not isinstance(v, str) or not v or '\0' in v or len(v) > 1024 for v in cmd)):
            raise ValueError('Guest commands must be bounded argv arrays')
    d['commands'] = commands
    if d.get('repo') is not None:
        repo = d['repo']
        if not isinstance(repo, dict) or set(repo) != {'url', 'commit'}:
            raise ValueError('Repo requires URL and full commit')
        u = urlsplit(repo['url'])
        if (u.scheme != 'https' or u.hostname != 'github.com' or u.username or u.password
                or u.port or u.query or u.fragment or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', u.path)
                or not re.fullmatch(r'[0-9a-f]{40}', repo['commit'])):
            raise ValueError('Only credential-free public GitHub HTTPS and exact commit')
    if len(json.dumps(d).encode()) > 15500:
        raise ValueError('Request exceeds the Nomad dispatch payload budget')
    return d


def validate_legacy(data):
    return validate_initialization(data, require_profile=True)

def validate(data):
    if isinstance(data,dict) and "image" in data:
        import universal
        return universal.validate(data)
    return validate_legacy(data)
