# Testing and merge protection

The current implementation is a local Slack/Conductor bridge. Flotilla's server,
remote clients, and plugin API remain a separate design. These tests protect
observable behavior while allowing those components to move.

## Boundaries to preserve

- Slack owns admission, thread mapping, reply placement, formatting, uploads, and
  notifications. `bridge.sqlite` is its durable delivery state.
- The Conductor adapter owns native IDs, commands, model configuration, connection
  generations, and pause intent. `native.sqlite` is its command journal. Startup
  receives a session/workspace mapping, not access to the Slack database.
- The poller consumes session observations through named methods rather than SQL.
  `test_observation_contract.py` delivers statuses and a recovered final answer
  with an observation provider that has no database/query interface. This is a
  useful seam for a future remote client; it is not a frozen network protocol.
- Native schema and SDK details belong in adapter tests. Keep broad behavior
  assertions about emitted replies, target sessions, preserved settings, and
  recovery, rather than private method call order or incidental table layout.

## Coverage map

| Contract | Tests |
| --- | --- |
| Owner/team admission, explicit mentions, new/follow-up/close, deduplication | `test_bridge.py` |
| Queue retains its reply; steer moves ownership and deletes old reply | `test_bridge.py`, `test_lifecycle.py`, `test_presentation.py` |
| Background callbacks, interrupted jobs, restart, no premature Done | `test_lifecycle.py`, `test_native_recovery.py` |
| Exact model/engine aliases, fallback, configuration frozen after acceptance | `test_native_adapter.py` |
| Actual JS dispatcher: configuration, queue/steer IDs, target rejection, pause intent, detach | `native_frontend.mjs`, invoked by pytest |
| Concurrent command claims, durable pause, lost acknowledgment, stale generations | `test_native_journal.py`, `test_connection_contract.py` |
| Fresh app/session observation; malformed localhost requests | `test_connection_contract.py`, `test_native_process.py` |
| Slack status delivery without Conductor SQL | `test_observation_contract.py` |
| Markdown pagination, source links, file/image delivery, upload recovery | `test_markdown.py`, `test_presentation.py`, `test_file_links.py`, `test_uploads.py`, `test_file_delivery.py` |
| Personal notifications, lost acknowledgment, restart, attention episodes | `test_notifications.py`, `test_notification_lifecycle.py` |
| Configuration, credentials, startup failure cleanup, launchd installation | `test_config.py`, `test_runtime_startup.py`, `test_service.py` |

`tests/support.py` contains common test setup; test modules do not import one
another. Disposable SQLite databases test real persistence. Injected clocks and
recording Slack/native boundaries avoid live accounts and production sessions.
The native HTTP tests use ephemeral loopback ports and close their servers.
Yield fixtures close shared SQLite connections, and resource/unraisable warnings
fail tests rather than hiding leaked database or socket handles.

Run only relevant tests locally, for example:

```sh
uv run pytest tests/test_native_recovery.py -q
uv run pytest tests/test_native_frontend.py -q
```

CI discovers **all** tests automatically on Linux/Python 3.12, Linux/Python 3.14,
and macOS/Python 3.12, with Node 22 for the JavaScript dispatcher. There is no
hand-maintained test-file allowlist. Reports include JUnit results and Python
coverage; coverage is diagnostic, not a target that encourages implementation-
mirroring tests. Python coverage does not measure `frontend.js`.

macOS CI exercises the mocked desktop boundary, not a live Conductor installation.
Vendor updates still need the focused live qualification documented in
[Native bridge](conductor-native-bridge.md). Automated checks do not establish
Accessibility permission, actual model availability, or sleep/wake behavior.

## Required CI and Graphite

The stable required check is `CI`, emitted by GitHub Actions (app ID 15368). It
runs even when dependencies fail and succeeds only when **every** test matrix
entry and lint job succeeds. Failed, cancelled, or skipped dependencies cannot
produce a successful gate. PRs run without path filters; main pushes and
Graphite `gtmq_*` branches also run checks.

[main-ruleset.json](../.github/main-ruleset.json) requires a PR and the `CI` check
on main, prevents force-push/deletion, and has no admin or app bypass. It does not
require another reviewer for this personal project. It uses the same aggregate
check pattern as TTD, without copying TTD's organization-specific bypass actors.

Checking in this JSON **does not activate protection**. On September 11, 2026,
GitHub rejected ruleset and branch-protection access for this private repository
with HTTP 403: the owning account needs GitHub Pro. Keep the repository private.
After upgrading, inspect existing rules before creating this rule:

```sh
gh api repos/zeddybot/slack-conductor/rulesets
gh api --method POST repos/zeddybot/slack-conductor/rulesets --input .github/main-ruleset.json
```

If this named ruleset already exists, update its ID with PUT rather than creating
a duplicate. Read it back after applying and confirm the effective main rules.
A failed disposable PR should show the required `CI` check as failing and merging
as blocked; a fixed revision should become eligible. Do not merge the canary.

Graphite projects trunk requirements onto a stack, so this GitHub rule also
supplies the CI requirement for normal Graphite merges. Strict up-to-date checks
are disabled, as in TTD. This requires passing CI but does not claim to test every
possible concurrent main update; introducing a dedicated merge queue is separate.
No GitHub-native queue is required here.

Sources: [Graphite repository configuration](https://graphite.com/docs/github-configuration-guidelines),
[GitHub required checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets),
[GitHub ruleset availability](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules).
