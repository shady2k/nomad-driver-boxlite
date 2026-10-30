ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# Official bundled source only; no similarly named package fetched from a registry.
RUN --network=none /usr/local/bin/uv pip install --offline --no-deps --python /opt/worker/prime/prime-agent-runtime/.venv/bin/python /opt/worker/prime/skills/agent-message && /opt/worker/prime/prime-agent-runtime/.venv/bin/python -I -c 'import agent_message,rlm,httpx; import importlib.metadata as m; assert m.version("agent-message")=="0.1.0"; print(agent_message.__file__)'
COPY check_agent_message.py /opt/worker/check_agent_message.py
