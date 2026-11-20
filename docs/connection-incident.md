# Socket Mode reconnect incident: September 9, 2026

A channel-thread mention at 15:00 EDT received no acknowledgment. The bridge's event store contained neither the request nor a session binding for the thread. This was a transport failure before agent dispatch, not a failure to support mentions inside other people's threads.

The daemon log showed repeated Socket Mode connection creation followed almost immediately by `BrokenPipeError`. The process remained alive, so launchd's KeepAlive policy did not restart it. SDK connection-established messages therefore did not establish healthy event delivery.

Restarting the LaunchAgent restored the Slack connection. The original request was then inserted into the normal durable intake using its original Slack message identity, after confirming it had never been accepted. It was dispatched once, created its Conductor workspace, and began updating the original Slack thread. The user confirmed it was working. This was recovery of one identified request, not a general channel-history replay.

## Watchdog added after the incident

The initial fix added a watchdog that observes heartbeat progress from the pinned built-in Socket Mode client. After 120 seconds without a new pong marker, it requests daemon shutdown. The normal loop exits after its current processing step, closes Socket Mode, and raises an error; launchd restarts the process. Session recovery uses the existing durable store.

Repeated socket replacement without any pong does not reset the watchdog deadline. Successful heartbeats keep a quiet connection healthy even when nobody sends messages. A brief disconnection followed by a healthy new session resets the deadline. The monitor exits promptly on ordinary shutdown.

Targeted validation covered repeated no-pong reconnects, an idle healthy connection, a stale pong, recovery onto a new session, monitor shutdown, and the existing bridge routing/delivery tests: 14 tests passed. Focused Ruff checks passed. The updated runtime was installed and the original investigation continued delivering live status after restart.

For the current recovery behavior, see [Slack connection recovery](slack-recovery.md).

## Limits

The trigger of the underlying SDK/network socket failures was not established. The watchdog addresses the process remaining stuck indefinitely despite failed automatic reconnects. It does not promise lossless Slack history recovery: requests never delivered to the bridge during an outage are not automatically backfilled. Native sends interrupted during a process restart retain the existing no-blind-replay policy.
