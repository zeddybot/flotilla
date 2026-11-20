# Flotilla branch qualification, September 29, 2026

This historical record covers stack #70–77, before the coordinated production
upgrade. The runtime and CLI revision tested was commit `99ca7cc`;
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
evidence. No GitHub settings or production services changed during this run.
The account billing limit blocked hosted Actions jobs before execution.
The stack maintenance pass found no review comments to address.

Private local artifacts are under `.context/flotilla/`: `ci-macos-final.log`,
`macos312.xml`, `linux-9524b8102cac-*`, `live-status.json`, and
`live-runtime-read.json`. They are intentionally not committed to git.

This run established automated regression and installation evidence. At the time, the
production cutover and an end-to-end check of new and existing sessions, queueing
and steering, background callbacks, file delivery, restart, and archive were
still pending. See the [README](../README.md) for current operations.
