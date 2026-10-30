# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM bash@sha256:eb33603676223a8765da42af35775e9cb68fb831b4903e7bb4c2d35646ef827b AS bash32
RUN mkdir /out && cp -L /usr/local/bin/bash /lib/ld-musl-aarch64.so.1 /usr/lib/libncursesw.so.6 /out/

ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS go-desktop
COPY apt-packages.lock /opt/worker-build-meta/
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d && apt-get update && xargs apt-get install -y --no-install-recommends < /opt/worker-build-meta/apt-packages.lock && rm -rf /var/lib/apt/lists/* && rm -f /etc/ssh/ssh_host_*
RUN curl --fail --show-error --location --connect-timeout 15 --max-time 600 https://go.dev/dl/go1.26.5.linux-arm64.tar.gz -o /tmp/go.tgz && echo 'fe4789e92b1f33358680864bbe8704289e7bb5fc207d80623c308935bd696d49  /tmp/go.tgz' | sha256sum -c - && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
RUN curl --fail --show-error --location --connect-timeout 15 --max-time 600 https://ziglang.org/download/0.16.0/zig-aarch64-linux-0.16.0.tar.xz -o /tmp/zig.txz && echo 'ea4b09bfb22ec6f6c6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17  /tmp/zig.txz' | sha256sum -c - && tar -C /opt -xJf /tmp/zig.txz && ln -s /opt/zig-aarch64-linux-0.16.0/zig /usr/local/bin/zig && rm /tmp/zig.txz
COPY --from=bash32 /out/ /opt/bash32/
RUN printf '#!/bin/sh\nexec /opt/bash32/ld-musl-aarch64.so.1 --library-path /opt/bash32 /opt/bash32/bash "$@"\n' > /usr/local/bin/bash32 && chmod +x /usr/local/bin/bash32 && bash32 --version | head -1 && zsh -c 'autoload -Uz compaudit; compaudit 2>/dev/null || true' | while read -r d; do [ -z "$d" ] || chmod -R go-w "$d"; done
COPY check-skills.py /opt/worker-build-meta/check-skills.py
RUN --network=none find /opt/worker/prime/skills -mindepth 2 -maxdepth 2 -name pyproject.toml -printf '%h\0' | xargs -0 /usr/local/bin/uv pip install --offline --no-deps --python /opt/worker/prime/prime-agent-runtime/.venv/bin/python && /opt/worker/prime/prime-agent-runtime/.venv/bin/python -I /opt/worker-build-meta/check-skills.py
ENV PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin
RUN ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt && go version && node --version && zig version && pkg-config --modversion gtk+-3.0 webkit2gtk-4.1 && dpkg-query -W > /opt/worker-build-meta/dpkg-packages.txt
LABEL org.opencontainers.image.title="Generic Prime ARM64 Go desktop worker" io.boxlite.worker-contract="2" io.boxlite.prime-version="0.10.1"

RUN test -x /usr/bin/unzip && cd /opt/worker/acpx && node --input-type=module -e 'import {createAcpRuntime} from "acpx/runtime"; if(typeof createAcpRuntime!=="function")process.exit(1)'
