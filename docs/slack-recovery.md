# Slack connection recovery

The September 11 incident started during overnight macOS maintenance wakes: a stale Slack socket was replaced, but replacements repeatedly failed with `BrokenPipeError`. The failures continued after the lid opened. Restarting the bridge daemon restored service while Conductor remained running. The old watchdog did not request a restart; logs did not contain enough watchdog state to establish exactly why.

Recovery now has independent safeguards:

- Both the service loop and a background worker evaluate connection health. An observation error requests a restart instead of silently killing the watchdog.
- A sleep interval of at least 30 seconds requests a fresh daemon. Detection compares an awake monotonic clock with a suspend-inclusive monotonic clock, so wall-clock corrections do not trigger restarts. macOS uses `CLOCK_MONOTONIC_RAW`; Linux uses `CLOCK_BOOTTIME`. These clock IDs have different semantics across platforms ([Apple](https://developer.apple.com/documentation/driverkit/mach_continuous_time), [Python](https://docs.python.org/3/library/time.html)).
- Five SDK transport errors request a restart. Only 30 seconds of advancing pongs on the same connection clears the error count; a lone pong on each failing replacement socket cannot keep the process alive indefinitely.
- Two minutes without observed pong progress also requests a restart. Merely creating a socket does not count as progress.
- Once shutdown is requested, a separate daemon thread allows 30 seconds for graceful cleanup, then exits the process even if socket teardown or non-daemon SDK workers are stuck. The guard remains active through interpreter shutdown. launchd's existing `KeepAlive` policy starts a fresh process, subject to its restart throttle.

Error diagnostics record exception types and counts; restart diagnostics include the reason. Error text is excluded from the new diagnostics because it can contain authenticated URLs. Existing SDK logging remains unchanged.

Restart affects the Slack bridge process, not the Conductor application or its agents. Committed events, thread bindings, status delivery state and native command identities remain in SQLite for recovery. This does not backfill Slack messages that never reached the daemon while disconnected.

## Validation

`tests/test_connection_health.py` covers simulated sleep, awake elapsed time, short sleeps, repeated errors with pongs, sustained recovery, stale-pong gaps, observation failures, SDK listener cleanup, and the real background worker with the platform clock.

`tests/test_runtime_startup.py` verifies that the service loop requests shutdown after transport failures before dispatching agent work, and releases acquired resources.

`tests/test_shutdown_deadline.py` launches disposable subprocesses with no Slack credentials or Conductor access. It verifies bounded termination with stuck work and leaked non-daemon threads, normal exit, no timeout during ordinary running, and SQLite recovery preserving committed events while rolling back an interrupted transaction.

Actual laptop sleep/wake remains a separate live qualification step. These tests inject the relevant conditions without suspending the developer's machine.
