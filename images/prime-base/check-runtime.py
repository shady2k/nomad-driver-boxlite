"""Offline image capability check. No daemon, prompt or model request."""
import importlib.metadata as metadata
import json
import pathlib
import platform
import subprocess
import sys

assert platform.machine() in {'x86_64', 'aarch64'}
assert sys.executable == '/opt/worker/prime/prime-agent-runtime/.venv/bin/python'
import rlm, agent_message, httpx, dill, pytest
import requests, yaml, tomli, dotenv, pandas, numpy, scipy, bs4, lxml, pydantic, tyro
assert metadata.version('agent-message') == '0.1.0'
versions = {}
for name, argv in {'prime': ['prime-agent', '--version'], 'acpx': ['acpx', '--version'], 'node': ['node', '--version'], 'uv': ['uv', '--version']}.items():
    versions[name] = subprocess.check_output(argv, timeout=30, text=True, stderr=subprocess.STDOUT).strip()
assert '0.10.1' in versions['prime'] and '0.21.0' in versions['acpx']
assert versions['node'] == 'v24.22.1'
assert pathlib.Path('/usr/bin/unzip').is_file()
assert pathlib.Path('/workspace/repo').is_dir()
assert not pathlib.Path('/root/.prime/nomad-persistent/models.json').exists()
print(json.dumps({'architecture': platform.machine(), 'versions': versions, 'agent_message': metadata.version('agent-message'), 'python_version': platform.python_version(), 'kernel_packages': {x: metadata.version(x) for x in ['requests','httpx','pyyaml','tomli','python-dotenv','pandas','numpy','scipy','beautifulsoup4','lxml','pydantic','tyro','dill','pytest']}, 'model_calls': 0, 'operator_config_baked_in': False}))
