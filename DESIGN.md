# Initial contract and boundaries

Target: Nomad 2.0.7 task-driver protocol and BoxLite 0.10.8 REST.
Language: Go, official Nomad plugin SDK; standard net/http for BoxLite. A tiny
CLI starts the Nomad plugin handshake. No separate controller or scheduler.

Planned files: cmd/nomad-driver-boxlite/main.go; internal/boxlite REST client and
contract tests; internal/driver Nomad plugin, durable task identity/recovery,
mock lifecycle tests; example jobspec; go.mod/go.sum. Credentials are node
bootstrap configuration (environment/secret-file), never task configuration,
argv, task handles, events or log messages.

Required lifecycle: fingerprint -> create named VM once -> attach before start
where required -> start -> inspect/wait -> signal/stop -> preserve disks on task
destroy unless explicit later policy permits deletion. Mapping allocation/task
ID to a stable BoxLite VM ID is persisted in the Nomad task handle. A deterministic
name plus durable pre-call intent closes uncertain-create/recover windows: never
blindly repeat non-idempotent REST POST or start a different VM. Recovery must
inspect the original VM or fail closed; no silent session replacement.

StartTask/RecoverTask/WaitTask/StopTask/DestroyTask/InspectTask and lifecycle events
are in scope. TaskStats maps only documented metrics with correct units, otherwise
reports unsupported/unavailable, never fabricated zeros. Signal/exec capability
only if safely mapped and explicitly advertised. Logs/native typed errors must
remain observable; bounded REST attach backlog is not durable full log storage.
No fake ready state: VM-running and application/ACP-ready are different facts.

Resources: approved task guest CPU count and RAM map to BoxLite create options;
Nomad reservation must cover guest RAM plus configured runtime overhead. CPU MHz
are accounting units, not guest vCPU equivalence. macOS has no Linux cgroup hard
reservation; BoxLite guest limits and cooperative scheduling are disclosed.
No host mounts, arbitrary node endpoint, image build, Prime/model credentials,
agent ACL expansion, live REST listener or production driver install.

Tests use httptest/fake REST only: create/start/wait/stop/recover, duplicate start,
uncertain create/start/stop, driver restart with same VM ID, typed errors/events,
resource validation and output handling. No real node-down injection. Upstream
source version and any unimplemented capability must be explicit in README.
