# Native Conductor bridge

The daemon and CLI use one native integration. The former Accessibility composer,
model-picker automation, queue-label detection, and archive-menu path are removed.
Model selection may still fall back to the configured default model, as requested;
there is no fallback to another control mechanism.

## Attachment and lifetime

A loopback HTTP server listens on a random port with an ephemeral secret capability.
The daemon briefly opens Inspector and installs a fixed dispatcher into the current
frontend module, discovered from the running page. It verifies initialized native
workspace/repository services before declaring readiness. Inspector receives a short
command that synchronously fetches the fixed dispatcher from the authenticated
loopback endpoint and evaluates it in the Inspector evaluation context. Conductor
blocks localhost module imports and asynchronous page-context eval through its
content security policy; the bootstrap does not alter that policy. There is no
arbitrary evaluation command in the transport, modified app binary, or competing
agent sidecar connection. The local fetch is synchronous only for this brief
bootstrap; command dispatch and agent work remain asynchronous.

Only Inspector bootstrap uses Accessibility/targeted keyboard input. Normal agent
operations leave the selected workspace, composer draft, attachments, and model
favorites alone. A fresh attachment is established after a frontend reload or app
restart. Heartbeats run independently of commands. Commands and results are fenced
by a host-issued generation and app process incarnation (PID plus launch time).

The launchd daemon owns the server, attachment supervisor, Socket Mode connection,
and status polling. Shutdown leaves the frontend installed. The daemon records its
loopback address, token and generation in `native-attachment.json`, and the next
daemon serves the same address so the frontend it inherited needs no bootstrap. A
daemon whose `frontend.js` differs starts a new generation instead, which retires
the old frontend.
The installer copies an exact source tree so deleted legacy modules are removed.
The attachment worker reads fresh process identity instead of relying on AppKit's
run-loop-dependent application cache. Focus restoration uses a fresh foreground PID
and only restores focus if the bootstrap still owns it. It retries with a longer
timeout on a slow machine. A bootstrap that cannot read the foreground app still
runs and leaves Conductor in front. On this Mac, helper shutdown
or reinstallation can itself foreground Conductor before bootstrap captures focus,
so restoration is best effort. The accepted startup tradeoff is a brief foreground
transition; ordinary message dispatch does not interact with the UI. After a failed attachment,
retries run after 1, 2 and then every 5 minutes whatever app is in front, because a
working bridge outranks keeping focus. With the screen locked, or once Conductor is
selected, they run every minute. Process
changes are still observed during backoff. Inspector lookup visits shallow controls
before deep logs, and checks AeroSpace placement even when an AXWindow exists.

The installation expression returns after starting the command loop. A rejected
heartbeat retires its attachment, preventing stale loops from filling Inspector
with failed requests. An unanswered one rests it after the release window, and a
daemon that is shutting down rests it at once: the user's pause, resume and send
go straight through, command polling stops, and a probe every 2 seconds waits for a daemon.
The one that answers resumes it and is told the latest action missed per session.
Re-attaching foregrounds Conductor, so the daemon only does it after a full grace
period of its own running time without a heartbeat; time lost to a machine stall
does not count, up to 2 minutes of silence. Detach is idempotent; a late callback cannot unwrap a newer
attachment. Shutdown commands are scoped to their attachment and retired before a
replacement can claim them, including records from older versions.

The local CLI shares the daemon's native command journal instead of attaching a
second controller. No special opt-in setting selects the native path.

### Conductor 0.86 compatibility and watch recovery

The 0.86 frontend uses `localWorkspaceId` for session ownership and workspace
operations. Its composer snapshot is at `composer.state.state.sessions`. SQLite
workspace lookup uses `workspaces.local_id`; `sessions.workspace_id` still stores
that local identity. The adapter exposes the local identity as its workspace `id`
so Slack bindings do not depend on Conductor's separate remote workspace ID.

The old session field rejected valid bindings on startup, and the uncaught error
caused launchd to restart the daemon repeatedly. Reattachment now isolates failures
per session, retries confirmed failures after 30 seconds, and preserves the command
identity for unconfirmed outcomes. Healthy sessions remain usable. Only validated
watches publish snapshots, so an invalid binding stays disconnected rather than
appearing idle. No failed watch is redirected to the selected desktop chat.

Validated on 0.86.0 on September 17, 2026: existing session watches, a new Luna
session through Slack DMs, queue and steer follow-ups in the same session, final
Done updates, and `[close]`. Regression tests also cover missing/archived records,
cached command errors, acknowledgement loss, and generation replacement.

## Delivery and identity

Incoming Slack events remain durable while Conductor is unavailable. Creation uses
a deterministic workspace ID recorded before calling the factory. A recovered event
reuses its Slack status and binds to that workspace's original session. Native sends
use a deterministic ID derived from the session and full Slack-tagged prompt.
Recovery checks accepted messages and outbox entries before retrying after a new
attachment. Native configuration is set at session start; follow-ups retain the
session's model/effort/fast configuration. Use `[new]` for new settings.

A retry has a delay so one interrupted thread cannot monopolize dispatch. Slack
status creation, notifications, and file delivery keep their existing persistence
and deduplication behavior. Slack and SQLite still cannot commit atomically; the
native journal does not claim universal exactly-once Slack delivery.

## Queue pause intent

For bridge-bound sessions, native pause/resume and explicit send entrypoints are
wrapped. Their intent reaches the durable local journal before the underlying
native action. Per-session serialization prevents reconnect restoration from
passing a concurrent pause. The host acknowledges only after its SQLite transaction
commits with synchronous durability.

Each intent has an identity and monotonically increasing revision. Replaying an old
send cannot overwrite a newer pause. A send interrupted before acceptance remains
blocked behind that pause until an explicit resume/new send. Reattachment reads the
latest journal entry at execution time rather than restoring a sampled heartbeat
flag. Unknown pre-existing pause intent is retained conservatively. Failed journal
writes do not acknowledge or apply the intercepted native action.

These wrappers apply to bound sessions while attached. They cannot observe user
controls in the interval before attachment; unknown intent must not be treated as
permission to resume. Native APIs may change with app updates and require tooling
updates; the bridge never falls back to sending keystrokes into the agent composer.

## Background work after a crash

The poller combines the durable transcript with fresh native runner status. SQLite
alone can say idle while a Codex runner is still working after reload. Missing or
stale live observations show Reconnecting and cannot conclude that a job died.

After an app-incarnation change, absent native background jobs are removed from the
outstanding set and the old interim result is invalidated. A still-working agent
shows Working and its new final response determines Done/Failed. If no agent remains,
the reply settles to Needs attention with the interruption identified. Stable native
absence also resolves a lost task callback after a short grace period. A helper-only
restart with the same app incarnation and live jobs preserves the outstanding work.
The bridge never silently reruns a killed job. Foreign task-result boundaries end
callback ownership, so a restored callback cannot swallow a follow-up or copy its
answer into the interrupted job's earlier reply.

Plan-mode callback recovery is intentionally outside scope. OS sleep/wake remains
excluded from the requested validation.

## Validation

Focused tests exercise durable pause acknowledgement, a crash before native pause
application, stale attachment responses, lost-send acknowledgements, absent-send
retry, blocked sends and explicit resume, session/workspace validation, native model
configuration, creation recovery, and post-crash background status. The actual
frontend dispatcher is exercised with controlled native-service and transport
boundaries. Focused startup regressions cover stale process identity, large
Inspector logs, offscreen AeroSpace windows, and process changes during backoff.

### Live qualification, September 10–11, 2026

The installed native-only runtime was exercised in two disposable owner-authored
Slack DM threads, using Fable and Luna without model fallback. Earlier all-model,
effort/fast, queue/steer, files, and notification qualification remains in
[the prior validation report](conductor-direct-validation.md).

- Pause crash: a one-shot fault exited the helper after committing pause revision 4
  but before acknowledging/applying native pause. The queue stayed paused after
  helper/app recovery; an explicit new send released it.
- Killed background job: after app exit, absent jobs stopped holding Slack in
  Waiting. A stopped agent settled to Needs attention with the interruption stated.
- Helper restart: a live background job survived, delivered its completion marker,
  and the queued request delivered a separate answer.
- Foreign callback recovery: a stopped job notification followed by a foreign
  result and the new request's answer reproduced two presentation defects. The
  regression fixes were verified against that sequence and in Slack: the old job
  reported interruption while the queued reply alone contained
  `native-recovery-separated-ok`.
- Clean app restart: the same daemon detected the replacement process, attached on
  the first attempt in approximately eight seconds, and delivered a Luna message
  sent during restart exactly once (`native-clean-restart-luna-ok`).
- Shutdown/restart: current shutdowns were acknowledged; two old unscoped shutdown
  records were retired automatically. Reattachment no longer replayed shutdown in
  a loop. A standalone bootstrap accepted a native command and detached cleanly.
- Cleanup: both new Slack test threads processed `[close]`; both corresponding
  native workspaces were verified archived. No fault controller remains active.

Private fault scripts, transcript observations, and results are in the gitignored
`.context/native-live` directory. Deliberately failed pre-fix test replies are kept
in the archived threads as evidence; they are not counted as passing checks.

The native integration is installed in the private launchd runtime. Plan-mode
callback recovery and macOS sleep/wake are intentionally unqualified.
