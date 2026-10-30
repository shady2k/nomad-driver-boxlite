# Runtime logging

Normal infrastructure logs contain request lifecycle, normalized tool progress,
plan metadata, terminal results, ACP control requests/responses and errors.
Per-chunk thought/message notifications and duplicate ACP tool notifications are
excluded before stdout, bridge logs and durable spool writes. Intended assistant
output and plans continue through the authenticated coordinator event channel;
thought text is never published. Tool counts are not a percentage of progress.

For a new bridge process, an operator may explicitly enable detailed notification
metadata with a protected `logging.json` in the existing operator state directory
(the parent directory of `companion`). Its complete schema is:

```json
{"chunk_metadata": true}
```

The file must be an operator-owned regular file with mode 0600, using the same
protected-file checks as other operator configuration. Missing means false;
unknown fields and non-boolean values fail validation. This is operator-only:
job payloads and prompts cannot select it. It never enables message/thought
content, tool arguments, prompts or credentials in infrastructure logs.
Remove the file or set false to restore normal logging for future bridges.
Existing processes do not reload it; do not restart active sessions merely to
change logging. No global log level or Nomad configuration change is required.

Raw stdout/stderr remains redacted and preserved in the protected diagnostic
spool. Before secrets readiness, non-JSON output is classified as bootstrap
output. Afterwards it is runtime_stdout/runtime_stderr, never mislabelled as a
secret-provider error. Full redacted traceback text remains in stderr/spool.
Replay cursors, normalized progress and terminal events are unchanged. Retention
and spool quotas are unchanged; this is filtering generated diagnostic metadata,
not deletion of existing logs.
