# Flotilla local operations

`flotilla status --json` reads the configured host's health snapshot. `flotilla doctor` exits nonzero if any check is warning or unknown. `flotilla monitor` records incident transitions locally; `flotilla monitor --watch` repeats at the selected interval (10 seconds by default, minimum 5). The service also samples every 30 seconds after its initial 30-second startup interval. `--root` selects a directory containing `config.toml`.

The server's authenticated `/status` endpoint returns the same health snapshot. It requires the private bearer token. `/health` reports application readiness. Neither endpoint is an external alerting service.

| Check | Warning condition |
| --- | --- |
| Server runtime and Slack | The running service reports either as not ready; a missing or stale service report is unknown. |
| Client remote link and native attachment | Remote status is older than 60 seconds or not connected; native heartbeat is older than 5 seconds or not ready. Missing status is unknown. |
| Service | The daemon lock is present but unheld. Missing lock is unknown. |
| Event backlog | More than 100 pending events, oldest pending event over 10 minutes, or a failed event within the last day. |
| Slack delivery | More than 100 deliveries in progress, a failed status within the last day, or one `delivering` status still present after 10 minutes of monitor samples. |
| Remote receipts | More than 100 unresolved calls, or one unresolved call still present after 10 minutes of monitor samples. |
| Notifications and asset cleanup | A queued notification or pending asset cleanup has at least 5 attempts. |
| Disk | At least 90% of the state volume is used. |
| Orphan snapshots | An upload directory has no matching status and is older than one day. |

Active or waiting agent work is counted and aged for visibility; its age alone is not a warning. Missing databases are unknown rather than created by a status read. Single-receipt and single-delivery stall detection begins with the first monitor sample and follows hashed durable IDs. It resets when an item resolves or disappears; a newly arrived item cannot inherit the old item's timer.

`monitoring.json` stores the latest snapshot, incident state, and hashed pending identities. `monitoring.log` records warning and recovery transitions once per incident; it rotates to `monitoring.log.1` above 1 MB. These files and the monitor lock are local and private (mode 0600). They contain counts and check details, not prompts, tokens, or raw native transcripts. No Slack messages or external alerts are sent by monitoring.

This local monitor cannot detect a complete host outage while the host is down. Check host availability from outside the machine if that matters operationally. Keep the state volume below the disk threshold and arrange off-host backups with a tested restore process; this document does not install a backup or external monitoring service.
