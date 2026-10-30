# Source, execution and result capabilities

- [Runtime/image discovery](#discover-runtime-and-infrastructure)
- [Four source methods and combinations](#select-how-source-enters-the-guest)
- [Results, lifecycle and diagnostics](#work-results-and-diagnostics)

Choose the method that matches the task, available data, history requirements,
network and freshness. No method has a mandatory priority. Toolchain availability
in an image does not prove that a repository/worktree has been transferred.
Set the command path explicitly before the examples:

```sh
CTL="${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py"
```

All CTL commands below run on the coordinator; source/build/test commands run
only inside the isolated guest. Variables are explicitly selected values, not
project defaults. Set CTL to `${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py`.

## Discover runtime and infrastructure

```sh
python3 "$CTL" resources
python3 "$CTL" nodes # explicit denied/incomplete when node:read is absent
python3 "$CTL" images --registry "$REGISTRY"
python3 "$CTL" images --registry "$REGISTRY" --repository "$REPOSITORY"
python3 "$CTL" request --architecture "$ARCH" --image "$IMAGE_AT_SHA256" --cpus "$CPUS" --memory-mib "$MEMORY_MIB"
python3 "$CTL" wait "$JOB" --until ready --deadline 300
```

Set ARCH explicitly to the verified Linux image architecture (`arm64` or `amd64`). For an OCI index, prefer its unambiguous returned `resolved_image` platform digest. Nomad chooses the node through architecture/driver constraints and resource reservations; do not pin NodeID by default. Host kernel Darwin/Linux is separate from the Linux guest OS. Inventory does not reserve capacity.

Read the returned immutable digest and platform. Catalog traversal is bounded
(first100 repositories/tags); narrow by repository when necessary. Denied,
empty, missing and unreachable are different outcomes. Registry metadata does
not certify a toolchain or source revision: use image documentation and guest
read-only checks. Read infrastructure resources; never change policy to fit a task.
Keep the returned DispatchedJobID, session_id and original allocation identity.
CPU/RAM are explicit inputs within operator policy, not project/resource profiles.

## Select how source enters the guest

| Method | Carries | Does not guarantee | Use/check |
| --- | --- | --- | --- |
| Selected worktree upload | Current regular file bytes, modified tracked files, nonignored untracked files, executable bit | Git history/config/hooks/index, deletion/rename semantics, arbitrary secret detection | Preview exclusions/manifest, apply receipt and hashes; no overwrite of different existing data |
| Git bundle | Explicit selected committed refs and their reachable history | Dirty/staged/untracked changes outside those commits, dependencies, safe historical contents | Review refs/history, SHA256, guest bundle verify and exact commit |
| Network clone | Public repository plus exact selected commit | Current coordinator dirty changes, offline success or credentials | Credential-free allowed URL, TLS/network/deadline/error checks |
| Image-provided snapshot | Whatever that immutable image explicitly packaged | Latest source, .git/history, task checkout or up-to-date dependencies | Check image docs/revision/lock metadata and content; copy to a fresh writable workspace |

A general runtime/toolchain image does not need project files. A project snapshot
or warmed dependency layer is optional explicit input, not the normal required
image design and not a project-to-image routing rule. No source preparation or
transfer happens automatically merely because a VM is ACP-ready.

### Selected worktree / directories

```sh
python3 "$CTL" upload "$JOB" --source "$SOURCE" --paths . --destination "$DESTINATION"
python3 "$CTL" upload "$JOB" --source "$SOURCE" --paths . --destination "$DESTINATION" --apply
```

Use an explicit real coordinator directory and safe relative guest destination.
Without --apply, this is preview only. Inspect path/count/size/SHA256/executable
manifest and exclusions before applying. A selected non-Git directory also works.
The destination is relative to the guest workspace; there is no --workdir,
--overwrite, --delete or live-mount/sync option. Omit --destination only when the
helper's fresh manifest-derived snapshots location is suitable; read its report.
An apply success returns committed:true, transfer_id, destination and local_manifest;
receipts live in the coordinator uploads/<session_id>/ directory. No receipt means
no proven completed transfer. A source changed during transfer is refused; do not
claim a coherent snapshot until manifest/commit acknowledgment succeeds.

The filter excludes .git, .env*, keys/credential config, node_modules, caches,
build outputs and other documented generated/private paths. Symlinks and special
files are refused. Known secret patterns are refused; this is not a complete
secret scanner. Review selected contents, including binary/archive contents.
Tracked deletions may cause current full-worktree preview to fail on a missing
path; upload is not a Git status replication protocol. Represent deletions/renames
with an explicitly reviewed Git patch when needed, and transfer selected untracked
files separately. Never silently omit that part of the dirty state.

New snapshots avoid stale data. Different existing destination files are refused;
identical data can resume. Do not overwrite a live working tree: stage separately
and ask the guest to apply a reviewed merge/conflict policy. No host mount, shared
Git configuration or guest SSH access to the coordinator is introduced.

### Git bundle + dirty worktree data

There is no dedicated bundle subcommand. A bundle, patch or archive is a regular
opaque file transported with upload/artifacts. Review selected history first:
compressed Git objects can contain old secrets and evade text-pattern filtering.
Do not package .git/config/hooks/credentials. Never pass GitHub write credentials
just to move commits between coordinator and guest.

For an explicitly selected coordinator repository, Git-only preparation is allowed;
never run project scripts/builds/tests or worker-provided commands there. Example
preparation commands (not executed automatically by the plugin):

```sh
git -C "$SOURCE" -c core.hooksPath=/dev/null -c core.fsmonitor=false rev-parse HEAD
git -C "$SOURCE" -c core.hooksPath=/dev/null -c core.fsmonitor=false bundle create "$STAGE/repo.bundle" HEAD
git -C "$SOURCE" -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv --binary HEAD -- > "$STAGE/changes.patch"
```

HEAD is an example selected ref; choose/review history for the task. A bundle
alone contains no dirty changes. Binary diff against the recorded HEAD captures
combined staged/unstaged tracked changes including deletions; it does not preserve
index-vs-worktree staging as separate states and excludes untracked files. Review
and separately select necessary untracked/nonignored files, then transfer them.
Do not claim Git index or all arbitrary hidden/ignored files were replicated.

```sh
python3 "$CTL" upload "$JOB" --source "$STAGE" --paths repo.bundle changes.patch --destination "$TRANSFER_DEST"
python3 "$CTL" upload "$JOB" --source "$STAGE" --paths repo.bundle changes.patch --destination "$TRANSFER_DEST" --apply
python3 "$CTL" upload "$JOB" --source "$SOURCE" --paths "$UNTRACKED_PATH" --destination "$UNTRACKED_DEST"
python3 "$CTL" upload "$JOB" --source "$SOURCE" --paths "$UNTRACKED_PATH" --destination "$UNTRACKED_DEST" --apply
```

Inside the guest, verify hashes, clone/import the local bundle into a fresh
checkout and run `git bundle verify` from a Git repository context (it requires
one). Verify the recorded commit, then `git apply --check`
and apply the reviewed patch. Import selected untracked files with an explicit
no-overwrite/conflict policy. This avoids a network clone. Alternatively overlay
a reviewed full file snapshot on a fresh bundle checkout, explicitly accounting
for deleted paths; do not apply the same dirty delta twice. Verify final file
hashes and Git diff/status before claiming dirty changes were preserved.

### Network clone

For initial startup, `request --input "$START_JSON"` accepts optional:
`{"repo":{"url":"https://github.com/OWNER/REPOSITORY.git","commit":"<40 lowercase hex>"}}`.
Combine it with the explicit image/CPU/RAM request arguments above. This is only
credential-free public GitHub HTTPS, not arbitrary remotes/SSH/credential URLs.
The guest bootstrap currently bounds clone120s and checkout60s. A slow source
can fail before ACP-ready. Do not increase deadlines, add credentials, bypass TLS
or change egress policy as a retry. A clone requested through native tools after
readiness is a separate guest operation, not this bootstrap; report its real
outcome, duration and commit rather than assuming the bootstrap limit applied.

Network clone does not transfer coordinator dirty files. Use worktree/patch data
in addition when required. Existing registry access does not prove Go/npm/package
endpoints work. Missing dependencies or TLS failures are explicit blockers, not a
reason to run builds on the coordinator.

### Image-provided source snapshot

Inspect documented revision/lock metadata and actual files in the guest. A
snapshot may intentionally omit .git; `git rev-parse` failure there is not proof
that no source is present. Toolchain caches/node_modules may be tied to specific
lockfiles. Do not treat the image as current checkout or automatically clone to
reconstruct .git: choose snapshot, bundle or network source per task. Do not
assume a project-specific prepare script exists in a general base image.

## Work, results and diagnostics

```sh
python3 "$CTL" continue --from-job "$JOB" --input "$TURN_JSON"
python3 "$CTL" wait "$JOB" --until result --request-id "$REQUEST_ID" --deadline 86400
python3 "$CTL" result "$JOB" "$REQUEST_ID"
python3 "$CTL" artifacts "$JOB" --paths "$PATCH_PATH" "$BUNDLE_PATH" "$REPORT_PATH"
python3 "$CTL" finish "$JOB"
```

Prepare patch/bundle/archive and build/test reports inside the guest, then export
while idle before close. artifacts accepts selected regular file paths, not an
implicit directory mirror. Keep returned manifest, sizes and SHA256 receipts;
verify the intended base/tip commit for a bundle and preserve uncommitted work
separately if not committed. Treat returned files as untrusted data: never execute
worker code or extract unreviewed archives on the coordinator. Git-only inspection
and controlled reviewed application are different from running project code.
No GitHub push is implied or needed. No automatic coordinator writeback is done.

Use `result JOB REQUEST_ID --saved` after close for an observed saved result.
Use `logs JOB --stream stdout|stderr` and `diagnostics JOB [--saved]` for launch/
terminal failures. Client log404 may mean GC; summaries can survive in server
TaskEvents and in a coordinator saved copy, not restored raw logs. If neither
exists, say diagnostics_unavailable. Do not redispatch an uncertain turn or
interpret running/low host CPU as completion. wait is an event subscription with
caller deadline, not shell polling or permission to restart a session.

### Read actual failure logs

```sh
python3 "$CTL" logs "$JOB" --stream stderr
python3 "$CTL" logs "$JOB" --stream stdout
python3 "$CTL" logs "$JOB" --stream stderr --saved
```

`logs` uses the existing scoped Nomad credential and does not require a dispatcher
seed read from OpenBao. It exports all currently retained stream bytes from the
start (up to the explicit256MiB/30s snapshot limit) to a protected0600 file on
the coordinator. Each capture is immutable; refreshing the latest pointer does not
delete previous snapshots. Read `local_path`, `bytes`, SHA256, `complete_snapshot`, capture
time and any limit/error. The returned inline text is the last64KiB, explicitly
marked as an excerpt when larger; it is not the whole file.

On observed bootstrap/application failure, `wait` drains the provider-exit event
with a bounded8s subscription, then captures both streams. This prevents the
failure event from hiding a traceback still being drained. `--saved` verifies
and reads the cached snapshot without contacting Nomad or OpenBao, including
after allocation GC. It is a snapshot, not recovery of lost logs or a guarantee
that future output was captured. If nobody captured raw logs before client GC,
the CLI reports that absence; a terminal summary is not a substitute for them.

The Mac also keeps redacted per-session text streams outside Nomad GC, including
non-JSON provider output and wrapper/guest bootstrap tracebacks. Segments rotate
at16MiB without overwriting/deleting earlier segments; the256MiB per-session
quota fails explicitly instead of silently dropping lines. These files and the
coordinator snapshots are retained until explicit operator cleanup. Uncaptured
Mac spool after GC is not exposed through a new endpoint or ACL. Do not claim
that `--saved` can retrieve it. Never blindly redispatch a failed/uncertain task.

### Distinguish job intent, allocation history and native runtime

`status JOB` includes the current `nomad.job_lifecycle` stop/status/version and
each allocation's JobVersion, terminal_history and older_job_version. A stopped
job with terminal history returns job_stopped; DesiredStatus=run on a retained
failed allocation does not override the job's current stop flag. `diagnostics`
also includes job lifecycle when no terminal summary exists. Missing diagnostics
are not evidence of a running VM. Native VM/PID state is explicitly unverified
by the scoped Nomad history API; an operator can independently verify suspected
orphan ownership without changing the job. Never remove history to fix display.

### Read current execution activity without private payload

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py" activity RETURNED_DISPATCHED_JOB_ID
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py" activity RETURNED_DISPATCHED_JOB_ID --follow --deadline 300 --after RETURNED_CURSOR
```

Replace RETURNED_DISPATCHED_JOB_ID with the actual DispatchedJobID from request;
use cursor from the previous activity response for RETURNED_CURSOR.
CLAUDE_PLUGIN_ROOT is the Claude plugin root, not a user-defined CTL alias.
If unavailable in the shell, use the installPath in your own
.claude/plugins/installed_plugins.json and append /scripts/workerctl.py.
`python3 FULL_INSTALL_PATH/scripts/workerctl.py --help` lists activity and options.
This is read-only: one status snapshot and one bounded subscription to the existing
authenticated event channel. It works with existing protocol2 sessions without a
VM/bridge restart. It returns request states, latest observed event and timestamp,
last observed tool/status, metadata and replay-gap/cursor information. It does not
print assistant text, thoughts, prompts, question content, files or tool arguments.
A completed tool does not prove every command inside it succeeded; no command
exit code is invented when ACP exposes only tool status. No events is unknown
activity, not evidence that the model is waiting or that a process is hung.

For new companion launches, the Nomad Logs tab also receives request accepted/
running/terminal, tool kind/status and measured tool/turn duration, with request/
session/allocation/VM correlation. Tool IDs are hashed; free-form tool titles and
arguments are omitted. `acp_trace` records observed acpx runtime operations,
prompt-written/turn-result boundaries, normalized session/update notifications,
and agent process lifecycle/error/timeout/disconnect metadata. Runtime operations
are labeled runtime_api; normalized notifications are not a raw JSON-RPC packet
capture. The pinned public runtime does not expose prompt JSON-RPC IDs or every
initialize/handshake message; rpc_id_available=false is explicit. No ACP payload,
model output, thinking or arbitrary error message is copied into these logs.

Already running companion/guest processes keep their loaded code. Updating the
plugin cannot retroactively populate their Nomad logs. Use activity for those
sessions; do not restart them just to activate logging. A newly created VM with
the installed trusted bootstrap receives the new assets without rebuilding an
image or restarting Nomad/driver. Nomad Overview/Recent Events remains primarily
driver/scheduler lifecycle; application execution metadata is in Logs.

## Image compatibility and display names

Use `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py" images --registry https://registry.example --workers-only --architecture arm64` to inspect registry metadata for a Linux ARM64 compute node. Pass the actual compute architecture, not the coordinator architecture. `--repository generic/worker` narrows discovery when catalog listing is unavailable. Denied, unreachable, empty and partially listed results are distinct.

`images` lists metadata, not runtime certification. A compatible declaration requires an exact platform and OCI label `io.boxlite.worker-contract=2`. Future image builds should also declare `io.boxlite.prime-version`. Contract 2 means a prepared Prime runtime, kernel and protocol-2 worker dependencies. Validate that contract during the image build and check guest readiness after launch. Missing labels remain in `unknown_images`; they are not silently rejected or accepted based on repository names. Existing unlabelled pinned images are unchanged. Multi-platform indexes resolve matching platform manifests; missing or conflicting contract declarations remain unknown.

A generic worker base plus upload and dependency installation inside the guest is the normal workflow. Project-specific images are an optional cache optimization. Installing packages does not turn AMD64 binaries into ARM64 binaries. Never run worker build or test code on the coordinator.

An optional explicit `--project "Project Alpha"` on `request` sets the Nomad display name to `project-alpha-worker-<request UUID>`. The immutable job ID stays `worker-<request UUID>`; use that returned ID for every subsequent command. The label is not inferred from a path or repository remote, is not a resource profile, and does not select an image. It accepts 1–64 ASCII letters, digits, spaces, underscores or hyphens, normalizes separators and rejects paths/URLs. Avoid private or secret labels. Existing jobs are never renamed.

## Read-only observation and capacity

`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workerctl.py" resources --live` separates node operator limits, current visible namespace reservations and conservative remaining resources. Guest RAM plus accounting overhead is distinct from measured host runtime RSS and from Nomad scheduler capacity. Missing node:read stays unknown; no extra permission is assumed. Session labels and states contain no prompts. Idle sessions reserve resources and require explicit project ownership before reuse.

`wait` reconnects read-only after transient transport/HTTP500-family failures with the last consumed event cursor, up to eight consecutive failures, delays capped at five seconds and the original caller deadline. Snapshot sequence heads never skip retained events. Epoch changes/replay gaps return explicitly. There is no prompt replay, VM replacement or unbounded retry. Use stage/result updates rather than tool counts or periodic shell polls.

Downloaded files are under the returned `files_directory`, which is `local_directory/files/`; the manifest remains at the transfer root. This is not a transfer-speed optimization.

## Operator model policy

When the installed `request --help` includes `--model`, the optional value is a model ID, never a gateway URL or credential. `models` reads the protected operator model policy and exposes only allowed/default IDs. Omission uses the operator default; an unknown model is rejected by fixed companion admission before create. Model choice participates in immutable request identity. Private `session-policy.json` supplies provider/default/allowlist; the operator distributes the same model policy to the coordinator. Existing sessions keep their selected model. A source-only feature is not activated on a running companion by changing documentation.

## ACP semantic messages and protocol limits

The [ACP v1 session update schema](https://agentclientprotocol.com/protocol/v1/prompt-turn) distinguishes visible agent message chunks, thought chunks, tool updates and plans. The pinned acpx runtime maps them separately. The [Prime0.10.1 adapter](https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.10.1/crates/pa-daemon/src/acp/types.rs) declares message/thought/tool variants but no structured plan variant; its [wire mapping](https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.10.1/crates/pa-daemon/src/acp/wire_events.rs) sends visible text deltas as agent messages.

`activity --follow --messages --deadline 60` explicitly includes intended agent messages and genuine plans from the authenticated stream. Default activity is metadata-only. Unknown streams and thoughts are excluded. Infrastructure logs receive plan availability/count/elapsed metadata, never plan text. A real plan is forwarded when present; the bridge does not infer one from tool calls. Ask for meaningful visible stage updates in the authorized task. Long synchronous tools may prevent new messages until completion. There is no spontaneous TUI wake-up or guaranteed heartbeat. Old guest processes keep their loaded code.
