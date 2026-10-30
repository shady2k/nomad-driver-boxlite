#!/bin/sh
# Guest image only: the host launcher does not execute this file.
set -eu
umask 077
test -f /opt/worker/.runtime-v1
test -f /opt/worker/.docker-default-v1
test ! -e /dev/kvm
test -w /sys/fs/cgroup/cgroup.procs
unset DOCKER_HOST DOCKER_CONTEXT LLM_API_KEY BOXLITE_DISPATCHER_SEED BOXLITE_API_KEY VAULT_TOKEN BAO_TOKEN
if docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then exit 0; fi
mkdir -p /dev/net /run /var/log
if [ ! -c /dev/net/tun ]; then mknod /dev/net/tun c 10 200; fi
chmod 600 /dev/net/tun
dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 --pidfile=/run/worker-docker.pid > /var/log/worker-docker.log 2>&1 &
daemon_pid=$!
tries=0
while [ "$tries" -lt 120 ]; do
 if docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then exit 0; fi
 if ! kill -0 "$daemon_pid" 2>/dev/null; then echo 'guest_docker_daemon_exited' >&2; exit 70; fi
 sleep .25
 tries=$((tries+1))
done
echo 'guest_docker_readiness_timeout' >&2
exit 70
