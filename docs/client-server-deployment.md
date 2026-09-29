# Single-owner Daytona pilot

The server owns Slack Socket Mode, `bridge.sqlite`, and `remote.sqlite`. The Mac
client owns Conductor access and `native.sqlite`. Keep the GitHub repository
private. Only the server receives Slack tokens; only the Mac has Conductor and
repository paths. Run exactly one Slack consumer during cutover.

## Build and start

Build a private snapshot from this checkout. The image contains code and locked
production Python dependencies, but no configuration or credentials:

```sh
daytona snapshot create slack-conductor-server --dockerfile Dockerfile.server \
  --context . --sandbox-class container --cpu 1 --memory 1 --disk 3
daytona create --name slack-conductor-server --snapshot slack-conductor-server \
  --target us --auto-stop 0 --auto-pause 0 \
  --auto-delete -1 --ttl 0 --public
daytona info slack-conductor-server -f json
```

With a snapshot, Daytona inherits CPU, memory, and disk from the snapshot;
the `daytona create` CLI rejects resource flags alongside `--snapshot`.
The explicit `--context .` is needed to include `src` and `scripts` in the
snapshot build.

The snapshot entrypoint waits for `/data/config/config.toml`, `.env`, and
`client.token`, then runs `sc --root /data/config server`. It restarts a failed
server after a bounded delay and forwards termination signals. A
`/data/config/HOLD` file asks the supervisor to stop the server (35-second
grace, then forced termination) and prevents restart until removed. Use it
before any server-side export or import:

```sh
daytona exec slack-conductor-server -- touch /data/config/HOLD
# Wait for the daemon lock to release; export/import refuses while active.
# After state operations:
daytona exec slack-conductor-server -- rm /data/config/HOLD
```

Daytona's CLI
does not upload files; use its Python SDK `sandbox.fs.upload_file` or an
authenticated SSH transfer to provision these three files and the migration
archive. Do not place secrets in the snapshot, Docker build context, command
arguments, or logs. Set `.env` and `client.token` to mode `600`; directories
containing them should be mode `700`.

Example `/data/config/config.toml` (all state lives on the sandbox's local disk):

```toml
mode = "server"
server_host = "0.0.0.0"
server_port = 8765
state_dir = "/data/state"
remote_token_file = "/data/config/client.token"
client_id = "mac"
```

The server `.env` contains `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`OWNER_SLACK_USER_ID`, and `SLACK_TEAM_ID`. Generate one random secret of at
least 32 characters for `client.token`; provision the same secret as a mode
`600` file on the Mac. The Mac's client config uses its existing Conductor and
repository paths, `mode = "client"`, the same `client_id`, and a `wss://`
`server_url` pointing to `/client` on the Daytona preview port.

The stable public preview address is
`wss://8765-<sandbox-id>.proxy.daytona.work/client`. `sandbox-id` comes from
`daytona info`; the exact proxy domain comes from Daytona's preview service.
Public preview routing removes Daytona's per-connection token requirement,
while the application requires its own bearer token on every route and the
WebSocket upgrade. Verify `GET /health` and `/client` without a bearer token
both return 401. Also verify Daytona's reserved toolbox port `2280` rejects
unauthenticated access. Do not put a Daytona API key or standard private
preview token on the Mac: the latter grants sandbox-wide terminal/toolbox
access and rotates on stop/start. A signed preview URL is port-scoped but
expires within 24 hours, so it is unsuitable as an unattended permanent URL.

## State cutover

The initial cutover from the retired all-in-one release stopped its Mac daemon
before export. For subsequent server moves or restores, put the current server
on `HOLD` before exporting. The exporter refuses an active `daemon.lock` and captures a consistent
SQLite backup. It includes thread bindings, pending events/statuses,
notifications, remote receipts when present, and referenced upload snapshots.
It excludes `native.sqlite`, native attachment files, and Conductor's database.
Treat the archive as a secret because it may contain messages and signed upload
URLs.

```sh
uv run --frozen python scripts/migrate_state.py export \
  --state-dir "$MAC_STATE_DIR" --output "$PRIVATE_BACKUP"
python /app/scripts/migrate_state.py import \
  --archive "$PRIVATE_BACKUP" --state-dir /data/state
```

Run the import command inside the sandbox after securely transferring the
archive. It rejects active server processes, unsafe archive paths, missing
snapshots, and checksum mismatches. The importer rewrites snapshot paths to
`/data/state/uploads/...` so interrupted Slack uploads can resume. If a
previous server state is deliberately being replaced, pass `--replace`; the
old databases and uploads move to `/data/state/migration-previous`. Keep the
original Mac state and archive until validation is complete.

After import, provision config and release `HOLD` to start the server, then
install/start the Mac client with `uv run python scripts/install_service.py`.
Check server health and the authenticated WebSocket, old-thread follow-up,
new-thread creation, queue and steer, notifications, file uploads (including
restart mid-upload), and recovery after client disconnect, server process
restart, and sandbox stop/start. A green health response alone does not prove
Slack or Conductor parity.

Rollback now means deploying a previous compatible server/client release.
There is no local mode or `--for-local` export option. Keep the server's current
delivery state authoritative; never replace it with the stale pre-cutover Mac
database after the server has accepted work. Put the server on `HOLD`, export a
backup, deploy the compatible server code, and reinstall the matching Mac client.
Release `HOLD` and verify health and a Slack round trip. Restore an older state
archive only as an explicit data-recovery action, because it can discard newer work.

For a server-host move, transfer the export and import it into the replacement
state directory while both servers are stopped. Completed snapshots are omitted;
unfinished Slack uploads retain their saved snapshots and file IDs. Partial
Mac-to-server downloads restart from the Mac's durable snapshot after a restore.
The migration archive does not contain Mac-native state or credentials. Preserve
the Mac client state and provision the replacement server credentials separately.

Daytona container filesystems survive stop/start, but processes do not. Disable
auto-stop and TTL as above, keep SQLite on local disk, and take regular
off-sandbox backups with a tested restore. Do not place SQLite WAL files on a
Daytona volume without validating its locking semantics. The project's
[transport trial](daytona-transport.md) established 30 minutes of WebSocket
connectivity, not multi-day server durability.

Daytona references: [CLI](https://www.daytona.io/docs/en/tools/cli/),
[preview authentication](https://www.daytona.io/docs/en/preview/),
[reserved ports and WebSocket support](https://www.daytona.io/docs/en/custom-preview-proxy/),
[persistence](https://www.daytona.io/docs/en/persistence/).
