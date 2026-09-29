# Single-owner client/server validation

The September 28, 2026 pilot is live: the existing private Conductor Slack app
uses one Daytona server and one native Mac client. No rebrand, multiplayer,
additional client implementation, or repository visibility change is included.
The [deployment guide](client-server-deployment.md) covers cutover and rollback.

The qualified application source is commit
`49163d8e519d9244aa3781af57d132fb8394d61f`. Later changes to this record and the
README are documentation only. The installed Mac source, server source, and
server wheel were checked against the release manifest. Private logs, hashes,
JUnit results, and Slack evidence live under `.context/deployment` and are
not committed.

## Automated checks

| Check | Result |
| --- | --- |
| Daytona Linux, Python 3.12.14 | 254 passed; Ruff check and format passed. Final source matches the workspace across 76 code, test, and lockfile paths. |
| Daytona Linux, Python 3.14.7 | 254 passed; Ruff check and format passed. |
| Focused Mac checks | Remote file failures/transport, state migration, and supervisor tests passed. No full suite was run locally. |
| GitHub Actions | Hosted jobs never started because account payments failed or the spending limit was reached. Local validation is the authorized merge path; the proposed ruleset is not active. |
| PR feedback | No actionable inline, review-body, or issue feedback across PRs 59–67 at the final audit. |

Automated coverage includes owner/mention admission, duplicate commands,
connection replacement, cursor recovery, queue/steer lifecycle, background
callbacks, notifications, file limits/integrity/retry/cleanup, interrupted
uploads, migration safety, and process supervision. These tests complement
the deployed-path checks below.

## Live Slack, Daytona, and Mac checks

All test prompts were confined to the owner's Conductor DM. All 13 fixture
workspaces were archived after testing.

| Check | Observed result |
| --- | --- |
| Existing thread migration | Pre-cutover and post-cutover replies used the same native session. The follow-up was sent once. |
| Mac offline admission | Daytona saved and acknowledged a real Slack request while the Mac client was stopped; reconnect delivered its exact expected answer. |
| Queue and restart | A running task and queued follow-up each completed after a Mac client restart and full Daytona stop/start. Each native send appeared once. |
| Steer | The old status became superseded, its Slack reply was deleted, the new reply completed below the steer, and only the final status notified. |
| Background callback | Slack stayed Waiting while a real background command ran. Its callback completed after a server SIGKILL and after `[new]` remapped the thread; the older session's answer still arrived. |
| New and close | New mappings used distinct workspaces; close archived mapped workspaces. Retired fixtures from earlier mappings were also archived. |
| Model selection | Fable, Opus, Sonnet, Haiku, Astra, Sol, Terra, and Luna produced expected replies with matching native model IDs. |
| Session settings | A new Sol session recorded high effort and fast mode enabled and returned the expected answer. |
| Files and formatting | A 1 MiB binary and inline PNG downloaded from Slack with matching hashes. A 100-row Markdown table appeared exactly once across two continuation pages. |
| Interrupted transfer | An 8 MiB transfer had a 3,407,872-byte partial file before the Mac client and Daytona were restarted. It completed with an exact downloaded-byte/hash match. |
| Missing file | The answer remained visible with a file-unavailable reason and Needs attention. |
| Notifications and cleanup | Terminal notifications were persisted and sent; the superseded steer did not notify. File snapshots were removed on both ends and cleanup work drained. |
| Authentication | Unauthenticated application requests and Daytona reserved ports rejected access. The Mac runtime has no Slack `.env`; it uses a separate bearer token. |
| Backup and restore | A quiesced post-cutover export restored successfully with SQLite integrity `ok`: 93 threads, 400 events, 396 statuses, 230 notification rows, no pending events or active statuses. The archive is stored off Daytona. |

Representative private Slack threads:
[existing thread, queue, restart](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1790644259601669),
[offline admission, files, steer](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1790645189257709),
[background callback, new mapping, settings](https://prodexlabs.slack.com/archives/D0C0HLKET9A/p1790645728182649).

## Remaining qualification and external dependencies

Alpha did not produce a result. The command reached a native ACP session with
`opencode:opencode/union-alpha`, then OpenCode reported that the model did not
load within 15 seconds. The alias mapping is unchanged from the pre-cutover
source, and Conductor's freshly discovered catalog contains no Alpha/Union
model. This is evidence of a provider availability problem, not proof that
Alpha works through the new deployment; do not count it as a pass.

Actual macOS sleep/wake, a Conductor application upgrade/restart, multi-day
Daytona durability, and a Slack-side network outage were not induced in this
run. Existing behavior still applies: the Mac must be awake for execution;
messages never received during a Slack outage have no automatic history
backfill; lost plan-approval callbacks are not reconstructed. Slack and SQLite
cannot provide an atomic exactly-once commit.

GitHub's hosted jobs did not start because of account billing. The repository's
proposed CI ruleset is not active, and Graphite's merge dry run reported the stack
eligible. The full local macOS/Python 3.12 run subsequently passed all 254 tests,
as did lint and formatting. Merge was authorized using local validation while
hosted CI is unavailable. Passing these checks does not establish zero possible
regressions.

The subsequent server/client-only change removes `mode = "local"`, `sc daemon`,
the installer's mode selector, and the legacy local-rollback export flag. Its
full macOS/Python 3.12 run passed 260 tests, including remote transport and
offline/reconnect behavior; Ruff lint/format passed. Validation and release
evidence are kept in the private deployment directory.
