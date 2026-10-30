# Nomad BoxLite driver and Prime worker plugin

A native Nomad task driver for isolated BoxLite VMs, with an optional Claude skill and fixed Prime ACP companion. Nomad is the scheduler. The caller supplies a pinned image and guest CPU/RAM; no project/profile catalog or submission service is required.

## Boundaries

- Operator-owned Nomad Variables provide per-node admission budgets. Missing, invalid or unavailable policy rejects new creates; existing work and recovery continue. Reservations include configured accounting overhead and are not measured usage.
- A durable single-allocation fence and persisted opaque driver state prevent silent VM replacement. Recovery reuses the owned VM rather than calling StartTask. This is node-local ownership, not a distributed multi-host coordinator.
- Agent jobs contain a BoxLite task; the host companion executable and bootstrap are fixed by the operator. Caller data cannot select host commands, mounts, templates, artifacts or arbitrary environment. Keep namespace driver restrictions and scoped ACLs in place.
- Session lifetime defaults to explicit close. Caller wait/transport deadlines are separate. Stateful results and bounded event replay support follow-up, cancellation, export and diagnostics without replaying uncertain prompts.
- TaskStats measures native host VM runtime CPU/RSS, not guest-used RAM. Missing samples stay missing; reservations are never substituted. Enable Nomad allocation telemetry for UI graphs. SignalTask is unsupported.

## Build and local tests

Use the Go version in `go.mod` and the pinned dependencies in `go.sum`:

```sh
go build -trimpath -o bin/nomad-driver-boxlite ./cmd/nomad-driver-boxlite
go build -trimpath -o bin/boxlite-stdio ./cmd/boxlite-stdio
go test ./internal/...
python3 scripts/test-python.py
node --test examples/prime/test_long_session.mjs examples/prime/test_terminal_metadata.mjs
```

Opt-in integration tests require an explicitly configured isolated endpoint and image. They are not enabled by ordinary tests. Guest build/test code must run inside the VM, never on the coordinator.

## Operator configuration

`examples/client.hcl` is a configuration example, not an activated deployment. Configure the local REST endpoint/provider, trusted companion path, node identity, namespace driver restrictions and operator policy before use. Do not grant arbitrary host execution or a broad job-submission namespace.

Private operator assets are outside this repository: node/source addresses, secret selectors, model-provider configuration, envconsul configuration, optional legacy HMAC/port bindings and network policy. The shared [Ansible role](provision/README.md) stages Linux/systemd and macOS/launchd nodes over SSH without starting services by default. Provide private host variables and pinned artifacts explicitly. The former Python installer is retired; no parallel installation implementation is maintained. The assets include `legacy-bindings.json`, `envconsul.hcl`, `models.json` and `session-policy.json`. A fresh deployment may have no legacy profile bindings. Preserve actual historical bindings when upgrading an existing deployment.

Secret values remain in the existing secret manager and process memory. Operator JSON contains selectors/configuration only, never API keys or tokens. Coordinator `environment.json` pins the HTTPS secret-manager origin; owner-protected `operator.json` selects the existing secret field path and CLI binary. Model policy uses allowed/default model IDs; requests never supply a provider URL or secret. No Consul service is required by this implementation.

## Claude worker skill

The plugin is in `examples/prime/claude-plugin`. See its [skill](examples/prime/claude-plugin/skills/nomad-worker/SKILL.md) and [capability guide](docs/WORKSPACE-CAPABILITIES.md) for executable usage. Use the installed plugin root reported by Claude, not a hardcoded host path.

`resources --live` reports visible namespace reservations and conservative remaining admission capacity, separately from scheduler totals and actual usage. `images --workers-only --architecture arm64` uses explicit OCI worker-contract/platform metadata and reports unknown images separately. Optional `request --project "Project Alpha"` changes only the display name; commands continue using the returned immutable job ID.

A generic image plus selected workspace upload or Git bundle is the normal path. Network clone and image-provided snapshots are alternatives with explicit freshness/history/network tradeoffs. Project-specific images are optional cache optimizations. [Generic Go/desktop recipe](images/go-desktop/README.md) contains no project source or project dependency cache.

`wait` uses blocking events and bounded read-only reconnect, not periodic status polling. Transient observation failure is not guest failure. Files download below the returned `files_directory`. Preserve manifests/hashes and do not execute received artifacts on the coordinator.

## Progress and limitations

The supported project components are this Nomad driver and its Claude worker
plugin. BoxLite runtime fork development has stopped. Official BoxLite 0.10.8
ships a guest kernel that does not provide the complete Docker networking
capabilities needed for full Docker network end-to-end operation; Docker network
e2e is not supported by this release. Its REST API has no guest privileged
setting. Installing Docker tools in an image does not remove these limitations.
See [runtime limitations](docs/guest-docker-runtime.md).

A custom netgo shim was installed privately to address Linux DNS. It is not a
stock BoxLite component, is not shipped here, and its behavior must not be
attributed to the official runtime. This repository does not publish a runtime
fork or custom guest firmware. Existing capability discovery fails closed when
a required runtime feature is unavailable.

User-facing assistant messages travel through the authenticated event channel; infrastructure logs contain only redacted metadata. Thoughts, prompts, private file contents and tool arguments are excluded from infrastructure logs. ACP plan support does not guarantee that the selected agent emits plans. The pinned Prime adapter emits visible message chunks and tool updates; stage messages must actually be produced by the agent. Synchronous tools can prevent mid-operation messages. There is no spontaneous Claude TUI wake callback.

Event replay, result/artifact quotas, transport deadlines and log retention are documented in the capability guide. Captured coordinator logs survive Nomad GC; uncaptured old logs cannot be reconstructed. Cross-project session reuse needs explicit ownership confirmation even when a session appears idle.

Early startup failures have a durable cleanup transaction. A new task may release its reservation after a definitive rejected create with confirmed absence, or after its exact owned VM is confirmed stopped. Disks and diagnostic receipts are retained. Lost create/start responses, ownership mismatches and unavailable observations keep the reservation for recovery; cleanup never replays create or start. A matching retry resumes only the pending cleanup.

For newly created Prime tasks, a trusted pre-ACP bootstrap failure receipt can stop the failed bootstrap automatically. A protected `application-ready.json` receipt prevents that path from treating a ready session as startup failure. Legacy sessions and ambiguous/runtime transport failures remain outside automatic bootstrap cleanup. This is failure cleanup, not idle timeout or automatic disk deletion. Rollback to an older strict state-schema reader requires an empty node and the verified pre-activation state backup.

## Optional exchange and manual cleanup

S3 is optional; direct uploads, artifact downloads and local export acknowledgement remain supported without S3 credentials. The worker skill documents explicit S3 upload/export/fetch, resumable multipart checkpoints and backend-bound receipts. Endpoint/bucket/secret selectors are operator configuration. S3 snapshots are not terminal task exports or agent memory checkpoints. Automatic VM close and disk GC remain disabled.

Operator-only `scripts/cleanup-stopped.py` previews by default and requires explicit VM IDs plus `--apply` for deletion. It refuses native disk deletion until all matching driver records are already Destroyed, then rechecks native stopped/PID, disk ownership, openers and driver locks. It never silently clears uncertain reservations; separately audited repair is required if lifecycle evidence and ledger disagree.

The generic [`images/prime-base`](images/prime-base/README.md) recipe supports amd64 and arm64 guests with checksum-pinned Prime/Node/uv artifacts, explicit worker-contract metadata and no project or operator credentials. Build and test an exact image digest separately from node provisioning.

## License

Original project code is [MIT licensed](LICENSE). Third-party reference files
retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md) and
the complete texts in [LICENSES](LICENSES/).
