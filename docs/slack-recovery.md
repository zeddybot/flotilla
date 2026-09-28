# Slack connection recovery

The September 11 incident started during overnight macOS maintenance wakes: a stale Slack socket was replaced, but replacements repeatedly failed with `BrokenPipeError`. The failures continued after the lid opened. Restarting the bridge daemon restored service while Conductor remained running. The old watchdog did not request a restart; logs did not contain enough watchdog state to establish exactly why.

Recovery now has independent safeguards. A lost connection is replaced inside the running daemon, because every daemon restart costs the Conductor attachment and its re-bootstrap foregrounds Conductor. A restart remains the fallback:

- Both the service loop and a background worker evaluate connection health. An observation error declares the connection lost instead of silently killing the watchdog.
- A sleep interval of at least 30 seconds requests a fresh connection. Detection compares an awake monotonic clock with a suspend-inclusive monotonic clock, so wall-clock corrections do not trigger restarts. macOS uses `CLOCK_MONOTONIC_RAW`; Linux uses `CLOCK_BOOTTIME`. These clock IDs have different semantics across platforms ([Apple](https://developer.apple.com/documentation/driverkit/mach_continuous_time), [Python](https://docs.python.org/3/library/time.html)).
- Five SDK transport errors request a fresh connection. Only 30 seconds of advancing pongs on the same connection clears the error count or counts as stable.
- Two minutes without observed pong progress also requests a fresh connection. Merely creating a socket does not count as progress.
- Replacement builds a new Socket Mode client and retries with backoff up to 60 seconds. The daemon restarts if the service loop has not replaced a lost connection within 60 seconds, or if no connection has been stable for 10 minutes: the September 11 replacements failed until a fresh process.
- Once shutdown is requested, a separate daemon thread allows 30 seconds for graceful cleanup, then exits the process even if socket teardown or non-daemon SDK workers are stuck. The guard remains active through interpreter shutdown. launchd's existing `KeepAlive` policy starts a fresh process, subject to its restart throttle.

Error diagnostics record exception types and counts; restart diagnostics include the reason. Error text is excluded from the new diagnostics because it can contain authenticated URLs. Existing SDK logging remains unchanged.

Restart affects the Slack bridge process, not the Conductor application or its agents. Committed events, thread bindings, status delivery state and native command identities remain in SQLite for recovery. This does not backfill Slack messages that never reached the daemon while disconnected.

## Validation

`tests/test_connection_health.py` covers simulated sleep, awake elapsed time, short sleeps, repeated errors with pongs, sustained recovery, stale-pong gaps, observation failures, SDK listener cleanup, and the real background worker with the platform clock.

`tests/test_runtime_startup.py` verifies that the service loop replaces a failed connection without stopping, waits for Slack while offline at startup, restarts after a sustained outage, and releases acquired resources.

`tests/test_shutdown_deadline.py` launches disposable subprocesses with no Slack credentials or Conductor access. It verifies bounded termination with stuck work and leaked non-daemon threads, normal exit, no timeout during ordinary running, and SQLite recovery preserving committed events while rolling back an interrupted transaction.

Actual laptop sleep/wake remains a separate live qualification step. These tests inject the relevant conditions without suspending the developer's machine.
