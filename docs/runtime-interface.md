# Session runtime boundary

Runtime contract **version 2** runs over transport version 2. Upgrade the server
and its client together: runtime v1 peers are rejected. This change adds bounded
replay and a completion gate; an older server must not consume the new client.
Conductor and the headless Claude/Codex worker implement the same session boundary.

The server owns Slack credentials, authorization, thread bindings, delivery, and
presentation. The client owns execution, engine credentials, workspace access,
file snapshots, and normalized observations. Runtime selection is configured with
`runtime_adapter = "conductor"` or `"headless"`. A server still owns exactly one
client connection; this is not multi-worker routing or a third-party plugin SDK.

| Operation | Contract |
| --- | --- |
| `probe` | Runtime version, readiness, capability flags and configuration policy |
| `sessions`, `describe` | Owned bindings, activity, configuration and pending native questions |
| `create(command_id, spec)` | Durable session creation; retry the same command ID |
| `recover(workspace_id, session_id)` | Validate and reattach an existing owned binding |
| `submit(command_id, session_id, message_id, run_id, prompt, options)` | Journal delivery; native acceptance and observed pickup remain distinct |
| `observe(session_id, after, limit, view)` | Bounded normalized replay and scoped snapshots/receipts |
| `close_session(command_id, session_id)` | Archive a session; not an alias for interrupt |
| `interrupt` | Cancel native work, preserving the session/workspace; report the observed acknowledgment accurately |
| `reveal` | Open the exact existing Conductor session; headless does not support interactive attachment |
| `respond` | Reply to an outstanding native request by ID; reject stale or foreign requests |
| `files` | Bounded, restart-safe file preparation/chunk/cleanup protocol |

## Identity, observations, and completion

Commands, input messages, logical runs, native segments and background jobs have
separate identities. Delayed Conductor steering joins the original run once its
pickup is observed. Persisted job ownership associates later callback segments
with the originating run. Legacy events without proven ownership retain null
message/run IDs; the adapter does not fabricate correlations.

Events are normalized before transport. Thinking text and nested/foreign output
are excluded. Conductor persists observed events in `runtime-events.sqlite`;
headless persists them in `headless-events.sqlite`. Conductor's original row IDs
remain opaque cursors so existing durable server receipts stay valid. An event
is protected from native-history deletion once it has been imported; history
removed before observation cannot be reconstructed.

Replay defaults to 200 events, accepts limits from 1 through 500, and bounds each
page by bytes. Consumers advance the returned cursor and honor `has_more`.
`session tail` drains pages; the server does not finish a reply while replay has
more pages. Retention is explicit maintenance after consumer acknowledgment,
not automatic deletion. An expired cursor raises a resynchronization error with
the retained boundary; it must never silently skip to the present.

Snapshots distinguish `fresh`, `stale`, and `unknown` observations. An idle
SQLite record or disconnected worker cannot prove completion. The `rest` field
separates a result candidate from observed completion, accounting for owned jobs
and callbacks. The server retains its durable per-reply lifecycle and settlement
window; it additionally requires fresh observations and the runtime's rest gate.
A later independently observed turn may allow an earlier reply to settle.

Conductor reports native model/effort/fast observations. Headless reports
`configuration.requested` separately from `configuration.effective`; effective
fields stay unknown unless the engine reports them. A session's requested
configuration is fixed at creation. Headless native conversation IDs are explicit:
Claude's UUID is reserved at creation, and Codex's emitted thread ID is persisted
before pickup acknowledgment. Resumption never uses a directory-global `--last`.

## Execution and controls

The headless service owns process creation and serializes work per session. CLI
commands use a private local socket rather than creating another process owner.
Each session gets an isolated Git worktree. Queueing and retries are durable;
steering, interactive reveal, and headless approval replies remain explicitly
unsupported. Process exit alone does not establish a successful answer: a
completion event and reconciliation of owned work are required.

Conductor cancellation targets the current native turn and pauses the queue.
Its native API acknowledges a request, not terminal process exit; `requested`
means observe the session afterward. Questions/approvals bind exact native IDs.
An acknowledgment lost across a frontend replacement remains unconfirmed rather
than replaying an approval or cancellation into newer work.

Headless stop cancels active and queued work and preserves the worktree. A fresh
input can continue an intentionally interrupted session. After an ambiguous
worker crash, affected work enters attention and is not rerun automatically.
Inspect the preserved native conversation/worktree before deciding how to
continue; there is no automated ambiguous-launch reconciliation command yet.
See [worker operation and qualification](headless-worker.md).

## Coordinated rollout

Preserve server `bridge.sqlite`/`remote.sqlite`, client command journals, runtime
event journals, workspace bindings, and private credentials. Do not restore an
old database over newly accepted commands without reconciling receipts.

The stack's tests and isolated native/Daytona qualification do not themselves
upgrade the production server/client pair. Keep rollback code and validate the
actual deployed versions, existing threads, queue/steer, callbacks, file delivery,
and archive after the coordinated rollout.
