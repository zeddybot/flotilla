# Session runtime boundary

This branch introduces runtime contract version 1 over transport version 2.
Conductor remains the only implemented runtime. The server still owns Slack
credentials, authorization, thread bindings, delivery, and presentation; the
client owns native execution, agent credentials, workspace access, and snapshots.

`runtime.py` defines the JSON-compatible operation contract. `remote_client.py`
dispatches it without invoking native methods directly. `runtime_adapter.py`
selects the integrated Conductor adapter; it is not yet an external plugin loader.
`conductor/session_runtime.py` owns native bindings, command receipts, session
specifications, message identity, and observation translation. A later headless
runtime can implement the same boundary without pretending to be a desktop app.

| Operation | Contract / current support |
| --- | --- |
| `probe` | Adapter version, readiness, capabilities, configuration policy |
| `sessions`, `describe` | Enumerate owned bindings and their current state/configuration |
| `create(command_id, spec)` | Create one durable session; repeat the same command safely |
| `recover(workspace_id, session_id)` | Validate and reattach an existing native binding |
| `submit(command_id, session_id, message_id, run_id, prompt, options)` | Native queue/steer with receipt reconciliation |
| `observe(session_id, after, ...)` | Replay normalized events with a durable source cursor; scoped snapshots/receipts |
| `close_session(command_id, session_id)` | Archive the bound Conductor workspace |
| `interrupt`, `reveal`, `respond` | Explicitly unsupported by this adapter until qualified |
| `files` | Existing bounded, restart-safe file snapshot/chunk/cleanup protocol |

Transport rejects the previous `native.*` and `db.*` RPCs. The server's
`RuntimeObservations` view preserves its existing durable poller interface over
`runtime.observe`. That compatibility view and the historical `RemoteConductor`
class name are internal; clients do not implement a Conductor database API.

Conductor transcript translation happens on the client. The wire carries input,
activity, output, tool/question, job-start/job-finish, and result events rather
than native transcript JSON. Thinking content and nested/foreign output are not
transported. Foreign result boundaries remain available for callback ownership.
Source row identifiers and native segment identifiers are opaque, not portable
engine semantics. Actual configuration is available in `describe`; final rest
and background callback tracking remain the server poller's qualified state
machine. A result event alone never establishes Done.

New sessions retain model/effort/fast settings from their creation specification.
A changed setting in a follow-up produces a visible instruction to create a new
session. Native selection failure retains the existing visible fallback policy;
the requested configuration is not presented as proof of the effective setting.
Queue/steer remains a per-message operation. Existing imported sessions preserve
their current native configuration behavior rather than inventing a past spec.

Commands, messages, runs, native segments, and jobs have separate identities.
The adapter journals commands and message-to-segment correlations. CLI retries
can supply `--command-id`; identical prompt text with distinct message IDs is
not collapsed. Submission receipts distinguish native acceptance from observed
pickup. Replaying a command returns its recorded acknowledgment, while fresh
observations show later progress. For pre-migration messages and callback
segments without a proven local correlation, `message_id`/`run_id` can be null;
the server retains its existing durable ownership tracking. Do not infer a new
logical run merely from a changed native segment ID.

Replay uses Conductor's retained local transcript, so deleting native history
also removes that replay source. This release does not introduce a second full
event store, standalone plugin process protocol, profile routing, or a stable
third-party SDK. Before advertising another adapter, qualify complete logical
run correlation, explicit freshness/configuration/rest events, replay retention,
and recovery against [the future-runtime plan](future-runtimes.md).

## Coordinated upgrade

Transport versions 1 and 2 deliberately cannot communicate. Upgrade the server
and client together during a controlled pause, preserving `bridge.sqlite`, both
`remote.sqlite` databases, native journals, and the client token. The new client
imports existing bindings and creates `runtime.sqlite`; it does not rewrite
Slack thread identities. Validate ready/auth/version agreement, observe an
existing session, then qualify new/send/replay/file/archive behavior before
resuming normal intake. Keep the previous server image and client runtime for
rollback. Restoring an old database over new accepted commands can duplicate or
lose work; reconcile durable receipts before restoring state.

The branch's test/installation evidence does not mean the deployed server and
Mac client have already been upgraded.
