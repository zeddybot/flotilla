# Follow-up work for Flotilla

The private pilot began with one Slack owner, a control server, and one Mac
client. The server now supports paired Mac and headless profiles, registered
collaborators, and runtime v2. See [client pairing](connect-client.md) and the
[current runtime contract](runtime-interface.md). The earlier worker pilot’s
execution and file-transfer results remain in the [worker qualification](headless-worker.md#initial-pilot-qualification).

## Operations and qualification

1. For each server/client release, confirm the deployed revisions and verify
   existing-session continuation, fresh sessions, queue and steer, reconnect
   replay, callback completion, file delivery, and archive. Preserve state and
   rollback artifacts. See [coordinated rollout](runtime-interface.md#coordinated-rollout).
2. Observe monitoring snapshots and incidents during normal use. An external
   availability check can cover full host outages. See [operations](operations.md).
3. Qualify actual Mac sleep/wake, Conductor restart and upgrade, Slack network
   outages, and multi-day Daytona operation. Record which native approvals can
   be recovered and the limits of Slack outage backfill.
4. Continue worker crash and restore qualification, and design an explicit
   recovery flow for native launches whose outcome cannot be determined.

## Off-host backups

Daytona retains a sandbox filesystem across stop/start and archival. A separate
backup can also protect against deletion, corruption, or a bad migration.
[Daytona persistence](https://www.daytona.io/docs/en/persistence/)

Back up the server’s delivery and thread databases and the clients’ command
journals and bindings. Keep credentials in a private recovery store. Use
SQLite’s backup mechanism on active databases, verify each backup, and rehearse
restoration in isolation. Before restoring either side, reconcile pending
commands so an earlier snapshot cannot cause already executed work to replay.
Check filesystem locking semantics before using an object-backed or FUSE
[Daytona volume](https://www.daytona.io/docs/en/volumes/) for live SQLite.

## Product follow-ups

- Qualify more Conductor question and approval recovery paths with live
  pending requests.
- Explore interactive attach to existing headless engine sessions. See the
  [future runtime design](future-runtimes.md).
- Maintain release and support procedures for clean public installs. See
  [public release preparation](public-release-plan.md).
