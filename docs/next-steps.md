# Next steps after the private client/server pilot

The supported deployment remains one Slack owner, a Daytona control server, and
one native Conductor Mac client. Server and client are the only service modes.
Implementation in a branch is not evidence that the deployed pair was upgraded.

## Implemented in the Flotilla follow-up branch

- A versioned session-runtime boundary and normalized events, implemented by Conductor and a headless Claude/Codex worker. Native DB methods no longer form
  the network API. See [runtime scope and limitations](runtime-interface.md).
- The `flotilla` CLI, scriptable single-owner init, config discovery/validation,
  session commands, human-readable output by default, explicit `--json`, and staged Mac installation. Existing `sc` and
  service/state names remain compatible. See [commands and installation](flotilla-cli.md).
- Local incident logs, health/status reporting, persistent delivery/receipt stall
  detection, and authenticated server status. No outbound monitoring alerts.
  See [operations](operations.md).
- Written [public-release preparation](public-release-plan.md) and
  [future-engine/host plan](future-runtimes.md). Neither changes GitHub visibility
  nor builds another client.

## Immediate qualification and operations

1. Review the runtime-v2 stack and schedule a coordinated server/client rollout.
   Transport remains version 2; runtime version changes from 1 to 2 and mixed
   runtime versions are rejected. Focused tests and isolated native/Daytona
   qualification cover the new implementation; they do not upgrade production.
   Preserve existing state and rollback artifacts. Validate existing-session
   continuation, fresh sessions, queue/steer, replay after reconnect, callbacks,
   file delivery, and archive through the deployed path.
2. Observe the deployed monitoring snapshots/incidents over normal work. Add an
   external availability check only when local logs/CLI status are insufficient;
   no local process can report its own machine being completely down.
3. Rotate the exposed Slack bot and Socket Mode tokens. Sameer will do this;
   install/validate replacements before retiring old credentials.
4. Retain the existing manual backup for now. Recommend a small scheduled,
   off-server backup and periodic restore exercise as the next reliability task;
   no new backup infrastructure is part of this change.
5. Qualify actual Mac sleep/wake, Conductor restart/upgrade, Slack network outage,
   and multi-day Daytona operation. Preserve the limits around Slack events
   never received and native approvals that cannot be reconstructed.
6. Resolve the unavailable OpenCode Alpha route explicitly, without mapping the
   name silently to an unrelated model.

## Why retain an off-server copy?

Daytona's sandbox filesystem persists across stop/start and archival. That is
useful durability, but it does not undo accidental deletion, database corruption,
or a bad migration. The [Daytona persistence documentation](https://www.daytona.io/docs/en/persistence/)
distinguishes sandbox lifetime from persistent storage. A backup should have a
separate deletion/failure boundary and a tested restoration procedure.

For this small single-owner service, prefer a daily SQLite-consistent backup of
the server's delivery/thread databases and essential deployment configuration,
with private/encrypted storage and a short retention window. Preserve separately
the client's native/remote/runtime command journals and binding state. Keep
credentials in an appropriate private recovery store, not a general source or
test artifact. Define how to reconcile pending commands before restoring either
side; a database restored to an earlier time can replay already executed work.

Use SQLite's backup mechanism rather than copying an active database file alone.
Validate row counts/integrity and rehearse restore in an isolated environment
without connecting a second Slack consumer. Keep active SQLite on a filesystem
with suitable locking semantics; an object/FUSE-backed
[Daytona volume](https://www.daytona.io/docs/en/volumes/) is not automatically a
drop-in WAL filesystem. Backup scheduling, destination, retention, and restore
automation remain follow-up decisions.

## Further product work

- Conductor reveal and interruption passed isolated live checks. Typed question,
  approval, plan, Codex-permission and MCP responses are implemented and tested;
  live pending-question qualification remains before calling all controls proven.
- Durable event replay, ownership correlation, freshness, configuration and rest
  gates now exist in both adapters. Continue crash/restore qualification and add
  an explicit operator reconciliation flow before wider unattended operation.
- The headless Daytona pilot passed real Claude/Codex create, queue, exact-ID
  resume, replay, reconnect and file-transfer checks. Claude worktree editing also
  passed. Codex nested sandboxing is incompatible with the tested default Daytona
  container. The selected `external-sandbox` policy now also passes Codex file
  editing, shell execution and resume after worker restart, using the Daytona
  box as the execution boundary. The worker example selects this policy
  explicitly; restricted policies remain available for other hosts.
- Publish the build-qualified image as a Daytona snapshot and qualify its
  dedicated server/worker deployment.
  The existing server allows one client, so this worker cannot simply join beside
  the Mac client. Keep [worker limitations and operations](headless-worker.md)
  explicit before expanding to another host or interactive attachment.
- Design pairing, per-device credentials/revocation, profiles, multiple clients,
  and collaborator permissions before implementing multiplayer routing.
- Prepare a clean public install/distribution and license/security documentation.
  Keep repository visibility, ruleset enforcement, Actions billing, and Slack app
  rebranding unchanged until separately authorized.

The broader proposals remain in [the design record](flotilla-design.md).
