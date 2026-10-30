# Remote BoxLite Nomad clients

Use the same declarative `boxlite_node` role for Linux/systemd and macOS/launchd.
Ansible connects through SSH; Nomad uses its own client RPC after provisioning.
No dispatcher service, custom scheduler, or installer daemon is introduced. The
companion and ACP gateway are internal task-driver protocol adapters.

Provide a private inventory and host variables outside this repository. Use an
existing SSH identity and service account. Never put API keys, Vault tokens,
private keys, or presigned URLs in inventory. Existing OpenBao identity files are
referenced by path; enrollment, policies, certificates and routes are separate
operator actions. Keep host routing and guest outbound policy separate.

Required host variables: `boxlite_service_user`, `boxlite_service_group`,
`boxlite_service_home`, `boxlite_advertise_ip`, `boxlite_source_ip`,
`boxlite_artifacts`, `boxlite_private_assets`, `boxlite_activation_config`,
`boxlite_operator_config`, and `boxlite_nomad_client_config`.

The six pinned artifacts are `nomad`, `boxlite`, `nomad-driver-boxlite`,
`boxlite-stdio`, `envconsul`, and `bao`. Each entry specifies `src`, `sha256`,
`version`, `os` (`linux`/`darwin`), and `arch` (`amd64`/`arm64`). Download official
releases and verify upstream checksums before supplying them. Custom driver and
stdio builds must have recorded source provenance and checksums. Cross-compiling
is not a substitute for a guest boot on the destination hypervisor.

Artifacts may optionally set `remote_src: true` when the exact pinned binary already exists on the target. The same role verifies its SHA256 on that target before copying it into a staged release. Controller-side files remain the default. Staging never executes these artifacts or changes the current release. A fresh-release check reports planned target copies; repeat checks verify idempotence.

Private assets contain `envconsul.hcl`, `legacy-bindings.json`, `models.json`,
and `session-policy.json`. They are references and policy, not embedded tokens.
S3 is optional; without its configuration the direct/local workflow remains.

```
ansible-playbook -i /private/inventory.yml provision/site.yml --limit worker-group --check
ansible-playbook -i /private/inventory.yml provision/site.yml --limit worker-group
```

The default action stages a content-addressed release without changing the loaded
services, current symlink, credentials, budgets, jobs, or node eligibility. Repeat
staging is idempotent. Changed code/config produces a separate release; previous
releases and task state are retained.

Activation requires `boxlite_node_activate=true` and
`boxlite_node_maintenance_confirmed=true`. The operator must establish an empty
maintenance window and quiesce existing Nomad/BoxLite agents first. The role
refuses activation while agents, guests, logmon or executor processes remain; it
does not stop them itself. Existing driver records also require explicit
`boxlite_reconciled_state_confirmed=true` after checking terminal ownership and
native VM state. This flag is an operator attestation, not automated proof of
safe cleanup. No state or disks are deleted. Staging does not need this window.

Linux uses systemd `KillMode=process`; macOS uses the existing logged-in user's
launchd domain with `AbandonProcessGroup=true`. Both require live recovery testing
for the installed runtime version. Do not infer loaded launchd settings from the
plist on disk. A macOS SSH-only host needs an existing GUI user domain; the role
does not create one or change login policy.

After activation, an open local HTTP port only proves local API availability.
Acceptance additionally requires official Nomad node inventory showing Ready,
healthy BoxLite, correct host architecture, an actual matching-architecture guest,
readiness/logs, transfer checksums and normal finish/resource release. Select
images with verified architecture and worker contract. Built-in host architecture
constraints are preferable to default NodeID pinning; no custom node scheduler is
provided here. CPU reservations are MHz, guest vCPU and host reserved RAM are
separate. Allow server RPC over the approved private route, not public REST.

`Nomad artifact` downloads initial files into the host allocation directory; it
is not an SSH control-plane transport and does not by itself copy into a BoxLite
guest. Existing authenticated guest transfer is still needed. Never embed an SSH
private key in a jobspec. The S3 input/output path is unchanged.

For automatic architecture placement, the plugin reads standard Nomad node details
and groups existing operator resource budgets by `cpu.arch`, datacenter, node pool
and driver API constraint. It does not choose a node or use free capacity as a
reservation. Nodes of the same architecture can have different MHz/vCPU policy;
the request reserves their maximum. The operator's MHz/vCPU guard remains the
admission contract, rather than a guessed physical clock frequency. The node API
supplies actual resources and architecture; no architecture table is persisted.

If the existing identity cannot read nodes (`node:read`), the CLI reports incomplete
inventory and retains the prior conservative maximum across all configured budgets.
This can over-reserve on a smaller host; it is not evidence that the host is full.
A missing or partial inventory never silently creates an architecture map or lowers
reservations. Keep dynamic session ports and the private adapter gate aligned with
the approved firewall range; do not install the public network-name placeholder.


### Optional verified runtime assets

The private operator configuration may select `runtime_assets` with a canonical
absolute `directory` and a `files` mapping of every asset basename to its SHA256.
The existing REST launcher validates ownership, protected parent directories,
regular non-writable files, executable modes and all fingerprints before reading
the REST credential. Symlinks, missing/unlisted assets and hash mismatches fail
closed. The runtime must include the shim and guest; Linux additionally requires
bwrap and libkrunfw.so.5. The launcher forwards only this verified directory as
`BOXLITE_RUNTIME_DIR`; inherited caller environment is ignored. Without this
optional setting, existing Linux and macOS launches are unchanged.

Use a distinct immutable asset directory and retain the official runtime for
rollback. A custom shim must retain its source revision, patch and checksum
provenance; it is not an official upstream fixed release. Activate only in an
established empty-node window and verify real guest DNS plus denied egress.
