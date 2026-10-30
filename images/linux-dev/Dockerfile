# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG DOCKER_VERSION
ARG CONTAINERD_VERSION
ARG BUILDX_VERSION
ARG COMPOSE_VERSION
ARG DOCKER_KEY_SHA256
ENV DEBIAN_FRONTEND=noninteractive GOTELEMETRY=off
# Image-only provisioning. No host socket, project checkout or credentials.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \
 && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg bc bison flex libelf-dev libssl-dev python3-pyelftools \
    sqlite3 iproute2 iputils-ping iptables nftables conntrack tcpdump kmod \
 && rm -rf /var/lib/apt/lists/*
COPY docker.asc /etc/apt/keyrings/docker.asc
RUN echo "$DOCKER_KEY_SHA256  /etc/apt/keyrings/docker.asc" | sha256sum -c - \
 && install -d -m 700 /tmp/docker-gpg \
 && gpg --homedir /tmp/docker-gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | grep -q 'fpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:' \
 && printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" "containerd.io=$CONTAINERD_VERSION" \
    "docker-buildx-plugin=$BUILDX_VERSION" "docker-compose-plugin=$COMPOSE_VERSION" \
 && rm -rf /var/lib/apt/lists/* /tmp/docker-gpg /root/.gnupg \
 && test ! -S /var/run/docker.sock
COPY start-docker.sh /opt/worker/start-docker.sh
RUN chmod 755 /opt/worker/start-docker.sh \
 && docker --version && dockerd --version && docker buildx version && docker compose version \
 && go version && node --version && python3 --version && sqlite3 --version && tc -V \
 && dpkg-query -W > /opt/toolchain-metadata/linux-dev-packages.txt \
 && test ! -e /root/.docker/config.json && test ! -e /workspace/repo/.git
ARG START_DOCKER_DEFAULT=0
RUN case "$START_DOCKER_DEFAULT" in 0) rm -f /opt/worker/.docker-default-v1 ;; 1) touch /opt/worker/.docker-default-v1 ;; *) exit 2 ;; esac
LABEL io.boxlite.worker-contract="2" io.boxlite.tool.docker.version="${DOCKER_VERSION}" \
 io.boxlite.tool.containerd.version="${CONTAINERD_VERSION}" io.boxlite.tool.sqlite3.available="true" \
 io.boxlite.tool.iproute2.available="true" io.boxlite.guest.docker-default="${START_DOCKER_DEFAULT}" \
 org.opencontainers.image.title="Linux development worker tools, no project repository"
WORKDIR /workspace/repo
CMD ["/bin/bash"]
