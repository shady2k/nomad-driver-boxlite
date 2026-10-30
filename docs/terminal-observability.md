Source-only terminal observability

The additive terminal_metadata field contains only allowlisted structured status,
stop/error codes, HTTP status, bounded attempt count and a SHA-256 of an upstream
request ID if the runtime actually supplies one. Missing fields remain null.
It never parses arbitrary error text or forwards raw headers, payloads or IDs.
Existing request_id already binds the terminal record to its turn. An ACP
end_turn is turn termination; task_completion remains unverified. Existing state
values stay backward compatible. A transport exception stays uncertain_no_retry;
partial text and completed tool records survive. No new POST retry exists.

Deployment: package session.mjs with the existing matching guest adapter release,
verify its checksum and run the regression suite, then update the normal Ansible
release source for future VM launches only. Do not replace files inside live
Mac guests or restart their processes. Keep previous release/image selectable
for rollback. Test one future bootstrap without a model call before considering
an independently authorized model acceptance. Existing sessions do not acquire
new metadata retroactively. Upstream IDs and finish reasons cannot be invented
when acpx/Prime does not expose them; preserving them at the authenticated
runtime boundary is a separate integration requirement.

Gateway findings (v2.0.0-rc.57, f6d6e9f26fa35668028dae62834b002a12f3c65e):
stream.go pumpStreamWithWatchdogObserver resets on every positive upstream
body.Read, not only a semantic token/event. A timer closes the upstream body on
idle expiry. It is distinct from total request timeout and client/VM lifetime.
Runtime default StreamIdleTimeout is 300 seconds; runtime setting
stream_idle_timeout overrides it, and group setting of the same name overrides
that. Existing logs establish timeout classification around 600 seconds but do
not reveal whether the effective value came from global or group configuration.
No secret environment or database access was used. To identify that exact origin,
an existing authorized operator must provide only the global stream_idle_timeout
and selected groups' override fields through the normal administration UI/API.
Do not increase the setting blindly: a silent upstream can otherwise occupy
resources longer without producing useful progress.

The requested public alias stays deepseek-v4.1-flash-auto. Gateway selection uses
a matching model's upstream ID and applies selected-group parameter overrides;
observed upstream IDs are deepseek-v4.1-flash and deepseek-flash. These IDs do not
prove any particular reasoning/effort parameter. Exact selected-group nonsecret
parameter overrides must be inspected through authorized admin configuration
before claiming what auto changes besides routing. No model setting was changed.
