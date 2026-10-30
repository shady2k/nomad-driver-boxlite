# Generic Prime worker base

Build from the repository root with a pinned Ubuntu 24.04 image digest:

```sh
docker buildx build --platform linux/amd64 \
  --build-arg BASE_IMAGE=ubuntu@sha256:REPLACE_WITH_VERIFIED_DIGEST \
  -f images/prime-base/Dockerfile --load -t prime-worker:local-amd64 .
```

The recipe supports amd64 and arm64 with architecture-specific verified official Prime, Node and uv artifacts. It includes acpx from the existing npm lockfile, the frozen Prime runtime lock, bundled agent-message, hash-locked full Prime 0.10.1 default kernel packages and unzip. Project dependencies, Bun and browser packages belong in the uploaded project workflow or an optional project layer.

No gateway, provider, model, credentials, project, host identity or S3 endpoint is baked into the image. The existing authenticated host bridge supplies launch assets. S3 remains optional. The capability label declares worker protocol 2; OCI platform metadata selects the guest architecture. Test the exact digest in a native BoxLite guest before deployment: Docker cross-build/offline tests do not prove KVM or authenticated Prime readiness.

Offline check after build:

```sh
docker run --rm --network none --platform linux/amd64 \
  --entrypoint /opt/worker/prime/prime-agent-runtime/.venv/bin/python \
  prime-worker:local-amd64 -I /opt/worker/check-runtime.py
```

The check imports runtime packages and reports tool versions. It does not start a model turn. The image contains no model policy; configure the permitted default and allowlist separately in operator assets.

The complete default kernel includes requests, httpx, PyYAML, tomli, python-dotenv, pandas, numpy, scipy, Beautiful Soup, lxml, pydantic and tyro, alongside the bundled RLM runtime and agent-message. The label `io.boxlite.kernel-defaults=prime0.10.1-full` identifies the checked recipe. A marker file or worker-contract label alone does not prove these imports or kernel execution work: bootstrap rechecks imports before ACP readiness. Validate an actual Python tool through Prime before a paid task.

Operator keys referenced by environment variable name must reach the trusted guest ACP process as well as the daemon. Never put key values in argv, image layers, task specs or diagnostic files. Internal stderr/error evidence is projected into bounded, payload-free `.nomad-diagnostics/runtime.jsonl`; collect it through the normal artifact reader while the session is available. It contains error categories/codes and fingerprints, never raw prompts, thoughts or tool arguments.

A private provider hostname needs explicit operator IP/CIDR authorization in BoxLite; hostname-only egress cannot silently grant private network access. Provider host aliases are derived only from that approved configuration. BoxLite IP/CIDR allowlists cover TCP and UDP without port selection, so any required port restriction belongs in the operator firewall. Shared ingress application authorization remains separate.
