# Flotilla branch qualification, September 29

Validation covers the implementation in stack #70–77, before any coordinated
production upgrade. The final runtime/CLI implementation is commit `99ca7cc`;
tests ran with the documentation layer applied. Subsequent qualification-note
edits do not alter the validated implementation.

| Check | Result |
| --- | --- |
| macOS, Python 3.12 | 300 tests passed, including actual isolated Mac runtime preparation and installed entrypoints |
| Linux, Python 3.12 | 298 passed, 2 Mac-only installation tests skipped |
| Linux, Python 3.14 | 298 passed, 2 Mac-only installation tests skipped |
| Ruff check and format | Passed on the full tree and independently on each submitted layer |
| First-run install | Separate configuration/source roots, final runtime symlink, console entrypoint, and managed wrapper exercised in isolation |
| Install failures | Build failure preserves the live runtime; bootstrap failure restores legacy directories and generation symlinks |
| Local transport | Actual authenticated WebSocket tests cover runtime operations, normalized events, reconnect, receipt replay, and duplicate-send protection |
| Monitoring | Authenticated HTTP status, unhealthy/recovered transitions, single-item 10-minute stalls, stale samples, and prompt/token exclusion checked |
| CLI JSON | Failed service commands return a redacted error envelope; install PATH advice stays inside the result |
| Existing live installation | New CLI read-only status confirmed native attachment, remote connection, daemon lock, and server readiness |
| Live native observation via v2 | Isolated localhost v2 server/client read an existing Conductor session through the new adapter; four normalized events, zero native control calls |

Linux ran in a disposable private Daytona CI sandbox, using Node 22 and the
repository workflow's frozen dependency/test commands. The uploaded allowlist
contained only source, tests, scripts, build metadata, and README; no local
config or credentials were included. The final bundle's per-file hashes matched
the local inputs. The disposable CI sandbox was deleted after downloading the
evidence. No GitHub settings or production services were changed. Hosted Actions
jobs remain blocked before execution by the account billing/spending limit;
they did not pass. The stack maintenance pass found no review comments to address.

Private local artifacts are under `.context/flotilla/`: `ci-macos-final.log`,
`macos312.xml`, `linux-9524b8102cac-*`, `live-status.json`, and
`live-runtime-read.json`. They are intentionally not committed to git.

This establishes automated regression and installation evidence, not a claim of
zero possible regressions. The new version has not yet completed a production
server/client cutover. Re-run a bounded end-to-end check of new/send, existing
session continuation, queue/steer, background callbacks, file delivery, restart,
and archive after the coordinated upgrade. Real sleep/wake, upgrade recovery,
prolonged outages, and multi-day operation remain soak-test work.
