# Next steps after the single-owner client/server MVP

The current product remains the private Conductor Slack app with one owner and
one native Mac client. The runtime supports only server and client modes. This
list records follow-ups; it does not claim they are implemented.

## Generalized session interface

Replace the Conductor-shaped network facade with a versioned session-runtime
contract while retaining Conductor as the only supported adapter initially:

- Explicit capability discovery and session-start model/effort/fast configuration.
- Create, recover, submit, observe, and close operations; respond, interrupt, and
  reveal only where the adapter establishes support.
- Distinct session, run, message, command, and background-job identities.
- Separate received, queued, and picked-up acknowledgments.
- Normalized output, activity, tool, question/approval, background-job, result,
  actual-configuration, and confirmed-rest events.
- Durable event replay and recovery snapshots; explicit stale/offline observations.
- Adapter-owned native identifiers and Conductor transcript translation.

Keep Slack policy and presentation on the server. Keep native execution,
credentials, recovery, and file snapshots on the Mac. Qualify the same existing
queue/steer, callback, notification, file, and restart contracts during extraction.
Additional adapters are a later step, not a prerequisite for this boundary.

## CLI polish and installation

- Install an executable usable from bash/zsh without a hand-written wrapper.
- Discover configuration independently of the current directory.
- Integrate client install/uninstall/status/start/stop/restart/logs and server
  operational commands with clear host-local semantics.
- Add config validation and consistent machine-readable JSON output.
- Define session list/show/open/stop separately from workspace archive.
- Keep setup scriptable; design pairing/profile/device commands only when their
  corresponding identity and routing capabilities are implemented.

## Operations and remaining qualification

- Restore GitHub Actions billing while the repository remains private. The
  complete local CI commands are the currently authorized merge validation.
- Enable and verify the checked-in main ruleset when the account/repository
  supports it; the JSON file alone does not enforce protection.
- Rotate the exposed Slack bot and Socket Mode tokens. Sameer will do this;
  update the server credentials and verify reconnection afterward.
- Automate recurring off-server backups, retention, and restore checks. Avoid
  making a routine backup silently drop Slack intake during a maintenance hold.
- Add operational monitoring for disconnects, delivery backlog, disk growth,
  stale snapshots/receipts, and prolonged server failure.
- Qualify actual Mac sleep/wake, Conductor restart/upgrade, Slack network outage,
  and multi-day Daytona operation. Preserve current limitations: no automatic
  history backfill for events never received and no reconstructed lost approvals.
- Resolve the unavailable OpenCode Alpha model explicitly; do not silently map
  the alias to an unrelated model.

## Later product work, requiring separate scope

- Prepare for a public repository soon, but keep it private until explicitly
  authorized. Review tracked files/history, personal configuration, credentials,
  licensing, and public setup documentation before publishing.
- Rebranding remains deferred.
- Multiplayer, per-user routing, device identities, profiles, and revocation
  remain deferred.
- Additional clients/hosts/engines remain deferred and require parity evidence.

The broader design and open decisions remain in [the design record](flotilla-design.md).
