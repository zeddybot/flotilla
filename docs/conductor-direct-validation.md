# Direct Conductor integration: 0.85 validation

Validated September 10, 2026 on macOS with Conductor 0.85.0. The second run used
this same Codex conversation in an independent tmux server, outside Conductor's
process tree. All Slack traffic was confined to the owner's Conductor bot DM.
OS sleep/wake was explicitly excluded.

This report records the earlier experimental qualification before the native-only
cutover. The production implementation, pause-race fix, and post-crash background
state follow-up are documented in [Native bridge](conductor-native-bridge.md).
Statements about the experimental harness and restored Accessibility bridge below
describe that earlier run, not the final implementation.

## Outcome

The tested attachment, dispatch, output, and recovery paths work after correcting
the issues below. There are two important native lifecycle constraints:

- A frontend reload can lose an outstanding plan-approval callback. Resolving the
  old callback then does nothing. Cancellation and a fresh follow-up restored the
  plan request, which could be rejected or approved normally. This is recovery
  through a new interaction, not transparent resumption of the old approval.
- Quitting Conductor can terminate its background shell jobs. The bridge continued
  observing the agent's report of that interruption and delivered the separate
  queued response. It did not silently rerun the job. Restarting just the helper
  and bridge preserved a background job through successful completion.

Productionizing the direct adapter must retain those distinctions. Deterministic
IDs and a reconnecting socket alone do not constitute a complete recovery design.
The tested fault windows are listed explicitly; this is not a proof covering
arbitrary process failures, disk failures, or every third-party agent error.

## Live verification

| Area | Result and evidence |
| --- | --- |
| Eight configured models | Fable, Opus, Sonnet, Haiku, Astra, Sol, Terra, and Luna each completed an actual owner DM through Socket Mode, Bridge routing, native Conductor execution, and final Slack delivery. No model fallback on these cases; native result metadata was saved. |
| Configuration | Native setters were exercised across 64 model/effort combinations, including invalid combinations, plus eight fast-mode cases with valid effort settings. This is configuration validation, not 72 complete agent runs. |
| Fast and effort | An actual `[new][astra][medium][fast]` DM created a new session and completed. The native result records requested and actual model/reasoning/service-tier metadata. |
| Model fallback | An intentionally unavailable test model completed on the configured default, with a warning. Normal model selection remained independent of the five favorites. |
| Optional-setting fallback | `[sonnet][ultracode][fast]` completed with high effort and standard speed, with explicit warnings for the unsupported options. |
| Thread identity | Follow-ups retained their native session. Selecting a second chat in the same workspace did not redirect the Slack message or inject anything into the selected chat. |
| Admission and validation | Direct invocations of the production admission logic rejected foreign users/teams, bot messages, edits, and unmentioned channel conversation. Duplicate event deliveries were deduplicated. Missing workspaces, mismatched sessions, and unowned workspaces were rejected. These were controlled admission tests, not messages sent as a coworker. |
| Initial attachment | The hook successfully read a known workspace and its session before advertising readiness. Inspector closed after attachment. |
| Frontend reload | A fresh attachment generation recovered automatically. An active Claude turn and its queued reply completed separately. |
| App restart | Guarded SIGTERM and SIGKILL tests restored Conductor and reattached. A final fenced restart confirmed the old app PID was gone while incoming Slack events remained durably pending; they completed after startup. |
| Work arriving offline | Both a follow-up and a new Haiku session were accepted while the app was down and processed after recovery. |
| Native outbox | A queued message retained across reload stayed queued past the old idle timeout and was subsequently delivered once. |
| Deliberate pause | A queue paused after enqueue remained paused across reload. It was not resumed until explicitly released, then completed once. |
| Active Codex across reload | Native SQLite status reset to idle while the sidecar still reported working. After adding live-status observation, a repeated shell-wait test remained active and delivered its final answer. |
| Lost send acknowledgement | An accepted send with its response deliberately discarded was reconciled against native storage. Exactly one native user message and one final response were found. |
| Crash during workspace creation | The bridge exited after native creation succeeded but before saving the binding. Recovery used the recorded workspace intent and resumed the original Slack status. Exactly one workspace, one session, and one native user message remained; `creation-crash-recovered-ok` reached Slack. |
| Interrupted before acceptance | A controlled reload destroyed the page before its delayed send. The test recorded zero native message/outbox entries before recovery. Recovery reused the command ID; one native message and the expected final response resulted. |
| Stale attachment | Commands and reports were fenced by the host-issued attachment generation and app PID. A delayed command rechecks its attachment before executing. |
| Helper/bridge restart | Both processes restarted while a native background job ran. The job completed and `helper-background-ok` reached Slack. |
| Notification crash | The bridge exited after Slack accepted a DM but before recording its timestamp. The persisted notification was `sending` with no timestamp. Restart recovered the original timestamp; Slack history contained one matching DM. |
| Questions | A question survived reload and accepted a native answer through the restored-question path, producing `question-recovered-beta`. |
| Plans | Normal rejection and subsequent approval worked. The approved plan created only its disposable fixture file and returned `plan-approved-ok`. Reload during an earlier plan exposed the lost-callback limitation above. |
| Tool permissions | With approvals enabled only for the disposable session's launch, a real Bash permission request reached `needs_user_permission`. Denial prevented execution; a subsequent approval completed with `permission-approved-ok`. Global permission settings were not changed. |
| Cancellation | A running Codex shell wait was cancelled. The bridge reported that it stopped without a final response; a follow-up completed in the same native session. |
| Background steer | The old Slack reply was deleted, the replacement timestamp followed the user's steer, and tracking continued until the agent reported the background job's interruption. |
| Queue presentation | The queued response had its own message after the queued user message and did not overwrite the steered response. |
| Markdown/files | A 6,664-character final reply with headings, emphasis, quote, table, and a long code fence was delivered in three pages. A text file and PNG were uploaded successfully. |
| Missing file | An intentionally absent attachment produced an attention state with the failed attachment identified, rather than a false successful upload. |
| Concurrent typing | Targeted keyboard events appended Unicode-containing draft text while another agent ran. The visible text, focus, and staged attachment remained intact; no unexpected navigation occurred during background sending or workspace creation. |
| Draft persistence | A separate draft set and flushed through Conductor's native draft service, plus its attachment, survived cold restart. The earlier DOM-input fixture was not a valid persistence test and is not used to claim crash-safe user typing. |
| Close/new | `[close]` archived a busy workspace with queued work. An ordinary follow-up was rejected. `[new][luna]` created a different session and completed successfully. |

Selected evidence threads:

- [Luna: baseline, pause, reload, and binding](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1789086104104999)
- [Fable: queue, questions, files, permissions, and background work](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1789086134079369)
- [Terra: delivery faults, close, and new](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1789086414706159)
- [Astra: notification crash and cancellation](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1789086413588929)

## Native model capabilities checked

These are Conductor 0.85's native capabilities. They do not add bracket commands
to the existing bridge. Unsupported settings must produce a warning and a supported
fallback rather than aborting an otherwise valid send.

| Model | Native effort levels checked as accepted | Fast |
| --- | --- | --- |
| Fable 5.1 | low, medium, high, xhigh, max, ultracode | no |
| Opus 5 | low, medium, high, xhigh, max, ultracode | yes |
| Sonnet 5 1M | low, medium, high, xhigh, max | no |
| Haiku 4.5 | omit effort | no |
| GPT-6 Astra | low, medium, high, xhigh, max, ultra | yes |
| GPT-5.6 Sol | low, medium, high, xhigh, max, ultra | yes |
| GPT-5.6 Terra | low, medium, high, xhigh, max, ultra | yes |
| GPT-5.6 Luna | low, medium, high, xhigh, max | yes |

The first fast-only setter probe omitted required effort and was rejected for
that reason. The table uses the corrected probe with valid effort settings.
Haiku rejects an explicit effort even though an inherited raw database column
may still contain an old value.

## Changes required by the experiments

### Attachment and transport

- Replace the old dynamic evaluator: 0.85's CSP rejects `AsyncFunction`. Use a fixed
  operation dispatcher; do not weaken CSP or modify the signed app binary.
- Read initialized services successfully before declaring ready. Method existence
  alone is insufficient. An additional attempted `started` property check was
  discarded because the exported composer is a wrapper without that property.
- Use real Inspector text input, verify execution, and close Inspector afterward.
  Attachment retries handled startup races without another app-stop action.
- Keep heartbeats independent of command execution. A slow asynchronous operation
  must not look like a dead frontend.
- Write heartbeat snapshots atomically. A torn JSON read crashed the first revised
  harness until its writer was corrected.
- Fence both polling and reporting with the current host-issued attachment and app
  incarnation. The initial forced-quit probe allowed a stale frontend to finish a
  delayed send; it was not counted as a pre-acceptance failure test.

### Delivery and observation

- Journal workspace creation intent before calling the native factory; recover the known workspace and its single session before sending the original prompt. A status left preparing must not be discarded merely because the bridge restarted.
- Journal message intent before dispatch. Use stable IDs and reconcile against both
  `session_messages` and `session_messages_outbox` before deciding to retry.
- Consult live native agent status when SQLite says idle after reload. The observed
  Codex process continued running and later emitted its final answer; relying on
  SQLite alone had already ended the Slack observation prematurely.
- Preserve explicit pause state. The harness restored previously unpaused queues
  and retained an observed deliberate pause. An unobserved pause racing a crash is
  not resolved by this test; production should retain ambiguous pauses and explain
  that they need attention rather than assume intent.
- Bind Slack to its stored session ID, independently of the UI's selected chat.
- Preserve pending plan state during transcript replay. A stored idle status does
  not mean an outstanding `ExitPlanMode` interaction was completed.
- Do not silently resubmit work killed by Conductor or approve a request whose
  native callback was lost. Surface the interruption and recover explicitly.

The source patch adds read-only native-outbox lookup and pending-plan replay to
the existing poller. It does not claim to install the experimental transport,
sidecar observation, queue resumption, or creation journal in production.

## Qualification limits

- OS sleep/wake was excluded at the user's request.
- Transparent recovery of an outstanding native plan callback is not supported by
  the inspected local API. Fresh interaction recovery was tested instead.
- Tool permission allow/deny was tested; a separate exhaustive crash-window matrix
  for every permission or MCP consent type was not run.
- The pause-intent race described above remains a production policy requirement.
- The experiment covered explicit send and notification failure boundaries, not
  arbitrary failures during every file-upload step or provider authentication.
- Exact injected exports are specific to this build. Keep interface checks and
  update the tooling when Conductor changes, as requested; do not block app updates
  solely because a build is absent from an allowlist.

## Harness safety and cleanup

The initial run's `launchctl submit` restart helper lacked a durable one-shot guard
and repeatedly killed Conductor. That failed run is not successful restart evidence.
Its jobs were removed and its restart/watch scripts remain disabled.

The second run used finite, explicitly invoked fault controllers. Each created its
single-use guard before stopping the app, held automatic attachment while the app
was down, and requested restoration within 45 seconds. No repeating launchd fault
job was installed. The testing agent remained in tmux throughout.

All 11 second-run Slack test threads processed `[close]`; all 14 disposable
workspaces were verified archived in native storage, including workspaces replaced
by `[new]`. No active or pending test status remained. The temporary frontend hook
was detached, the supervisor/bridge/transport processes stopped, and the connection
credential removed. The ordinary production LaunchAgent was restored and verified
running after the experimental Socket Mode connection closed. The existing
Accessibility adapter remains the installed production path.
Private logs, IDs, tokens, raw Slack messages, and prototype scripts remain under
`.context/direct-recovery/`, which is gitignored. The source report contains no
credentials. No native Conductor database or app binary was edited directly.

Fixture permission configuration followed the documented
[Claude Code permission rules](https://code.claude.com/docs/en/permissions) and
[PreToolUse hook contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control).
Those temporary files were confined to a disposable workspace and restored afterward.
