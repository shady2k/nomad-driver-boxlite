# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PI_OFFLINE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 python3-venv unzip xz-utils bash procps ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY examples/prime/guest/package.json examples/prime/guest/package-lock.json /opt/worker/acpx/
COPY examples/prime/guest/uv.lock examples/prime/guest/provision.sh /opt/worker/assets/
COPY images/prime-base/kernel.lock /opt/worker/assets/kernel.lock
RUN /bin/sh /opt/worker/assets/provision.sh \
    && uv pip install --python /opt/worker/prime/prime-agent-runtime/.venv/bin/python --require-hashes -r /opt/worker/assets/kernel.lock \
    && uv pip install --offline --no-build-isolation --no-deps --python /opt/worker/prime/prime-agent-runtime/.venv/bin/python /opt/worker/prime/skills/agent-message \
    && rm -f /opt/worker/prime.tar.gz /opt/worker/node.tar.xz /opt/worker/uv-*.whl
COPY images/prime-base/check-runtime.py /opt/worker/check-runtime.py
RUN --network=none /opt/worker/prime/prime-agent-runtime/.venv/bin/python -I /opt/worker/check-runtime.py \
    && touch /opt/worker/.runtime-v1 /opt/worker/.kernel-defaults-v1
LABEL io.boxlite.worker-contract="2" \
      io.boxlite.prime-version="0.10.1" \
      io.boxlite.acpx-version="0.21.0" \
      io.boxlite.kernel-defaults="prime0.10.1-full" \
      org.opencontainers.image.title="Generic Prime worker runtime"
WORKDIR /workspace/repo
CMD ["/bin/bash"]
