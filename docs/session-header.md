Session header fix (source and staged release only)

For each new guest bootstrap, the existing ACP bridge renders Prime provider-level
models.json headers with x-opencode-session equal to request.session_id (canonical
durable UUID). This is a conversation routing label, not an authentication token.
It remains unchanged across turns/retries and differs between sessions. All other
provider fields/headers are preserved; stale case-insensitive duplicates are
removed. No endpoint, credentials, model or permission is added.

terminal_metadata is persisted in the guest result and projected through the
existing authenticated journal with a strict categorical allowlist. Arbitrary
nested guest data cannot assert task verification or enable automatic replay.

Tests: actual Prime0.10.1/acpx0.21.0 in the existing image, external network
disabled, emitted four HTTP requests in two conversations. The synthetic endpoint
verified stable per-conversation headers and distinct values between conversations;
all four normal SSE turns completed. Separate session tests check error, incomplete
and EOF after output/tools. This is NOT a live GPT-Load/provider acceptance. GPT-Load's pinned source clones end-to-end request headers; that
forwarding contract has been read, but the running gateway has not received a new
model request from this release. No claim is made that this fixes provider503.

The normal Ansible stage role packages the release with activation=false. It does
not select current, restart agents, update live Mac guests, or make future launches
use the release until a separate safe adapter activation is completed. Do not use
the broad runtime activation path merely to deploy this adapter while VMs run.
