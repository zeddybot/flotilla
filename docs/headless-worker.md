# Run a headless worker

The headless client runs Claude or Codex in a repository on its host. It can share the Slack server with Conductor and other paired clients. Pair each worker with a separate credential and profile using the [client setup guide](connect-client.md). The operator can also provision a dedicated standalone control server as described below.

## Build and provision

Members of the Daytona organization can access running sandboxes, including
their files and credentials. Choose the organization accordingly. The sandbox's
public setting controls preview access. [Daytona organizations](https://www.daytona.io/docs/en/organizations/)

From the repository checkout, build a private Daytona snapshot using the pinned tools in [Dockerfile.worker](../Dockerfile.worker):

```sh
daytona snapshot create flotilla-headless-worker --dockerfile Dockerfile.worker \
  --context . --sandbox-class container --cpu 2 --memory 4 --disk 10
daytona create --name flotilla-headless-worker --snapshot flotilla-headless-worker \
  --target us --auto-stop 0 --auto-delete -1 --ttl 0
```

These snapshot and create flags are in [Daytona's CLI reference](https://www.daytona.io/docs/en/tools/cli/). The worker needs no public inbound port. Check out the repository at `/data/repos/project` and run the [headless pairing steps](connect-client.md#pair-a-headless-daytona-client) from the worker shell. If the image already provides the installed CLI, run `flotilla` directly. Pairing writes `config.toml` and `client.token` under the worker’s config root.

For a dedicated standalone server, provision `/data/config/config.toml` from [config.worker.example.toml](../config.worker.example.toml) and install a matching application token at `/data/config/client.token` on the worker. Set the token file to mode `600` and private directories to `700`, owned by the container's `worker` user. Use [Daytona's SDK file upload](https://www.daytona.io/docs/en/python-sdk/sync/file-system/) or authenticated SSH to provision files. Keep credentials out of the image, build context, command arguments, and logs.

Provide engine authentication to the worker user separately. Claude can use `ANTHROPIC_API_KEY`; Codex can use a private `CODEX_HOME` with its existing authentication. Keep values out of `config.toml` and the snapshot. The worker image pins Claude Code `2.1.327` and Codex `0.166.0`; it does not provide credentials. For manual standalone provisioning, set `server_url` to the dedicated control server's `wss://…/client` URL, `repo` to the repository's absolute path, and `client_id` to the identity configured on that server. The Daytona example selects Codex with `engine_permission_policy = "external-sandbox"` and `send_mode = "steer"`. Codex uses the dedicated Daytona box as its execution boundary. In Slack, use `[queue]` on a single message to run it after the active turn.

The image entrypoint waits until `config.toml` exists, validates the config and application token, then supervises the client. On an unexpected client exit it restarts with bounded backoff. A `/data/config/HOLD` file stops the child and prevents restart until removed. Use `HOLD` before state backup or maintenance:

```sh
daytona exec flotilla-headless-worker -- touch /data/config/HOLD
# Verify the client has stopped, then perform maintenance.
daytona exec flotilla-headless-worker -- rm /data/config/HOLD
```

The supervisor gives the child 35 seconds after a stop request, then kills it if needed. Stopping a worker while a turn is launching or running creates an attention state on restart; it does not automatically repeat that prompt.

## Operate sessions

Run `flotilla` on the worker with its config root, either through a terminal in the sandbox or a secure remote command. The CLI uses the running worker's local runtime service.

```sh
flotilla --root /data/config status
flotilla --root /data/config doctor
flotilla --root /data/config session new 'inspect the repository' --engine claude --model claude-sonnet-5-5 --queue
flotilla --root /data/config session list
flotilla --root /data/config session show SESSION_ID
flotilla --root /data/config session tail SESSION_ID --follow
flotilla --root /data/config session send SESSION_ID 'check the tests' --queue
flotilla --root /data/config session archive SESSION_ID
```

Use `--engine codex --model gpt-6.2-sol` to request GPT-6.2 Sol. `--model` selects a model for that session; the requested choice and any model reported by the native engine are shown separately. Add `--json` to commands used by scripts. With the example workspace root, each session gets its own git worktree under `/data/workspaces/headless-sessions/`. Queued messages run one turn at a time per session. Use `session send SESSION_ID "new instruction" --steer` to update the active Claude or Codex turn. The worker records pickup after the native engine acknowledges the instruction. Use `session tail` to observe the transcript. Interactive session attachment and approval responses require Conductor. Archive stops active work and retains the worktree for inspection.

`read-only` and `workspace-write` retain engine permission enforcement. The former restricts edits; the latter permits worktree writes subject to the engine's normal noninteractive approval behavior. Neither policy enables blanket permission bypass. An unanswerable question or approval must become an attention state. If a launch may have reached the native CLI before the worker lost its acknowledgment, the runtime blocks that session for manual reconciliation. Inspect the event journal, native session ID, engine history, and worktree before deciding whether to resume or abandon it; no automatic retry is safe. The current CLI has no general command that clears an ambiguous launch.

## Preserve state

Keep the worker sandbox persistent across stop/start and store SQLite on its local filesystem. With the worker held and the client stopped, back up `/data/state/headless.sqlite`, `/data/state/headless-events.sqlite`, `/data/state/remote.sqlite`, `/data/state/remote_assets/`, the worktrees, and the engine's native session and authentication state in the worker user's home. Preserve `/data/config/config.toml` and `client.token` securely as separate secrets. Restore these pieces together so command receipts, event cursors, native IDs, and workspaces still agree. The server has separate Slack delivery and binding state that needs its own backup; the existing server migration archive does not cover the worker's new databases or native engine history. Daytona documents sandbox filesystem [persistence](https://www.daytona.io/docs/en/persistence/), but a stopped container does not preserve running processes.

## Daytona sandbox compatibility

The live pilot found that Daytona's default container blocks the nested namespace
operations Codex uses for its Linux sandbox (`bwrap` failed while setting up
loopback or UID mappings). Installing bubblewrap alone did not resolve this.
Restricted Codex text/resume works, but file-tool execution needs a compatible
inner sandbox or an explicitly selected outer execution boundary.

`engine_permission_policy = "external-sandbox"` is the selected Daytona policy for an isolated
Linux worker marked `FLOTILLA_ISOLATED_WORKER=1`. It uses the worker container as
the execution boundary: the Codex app-server receives the `danger-full-access` sandbox mode, and
Claude receives `--permission-mode bypassPermissions`. This allows tools to act
throughout that container, including its available credentials and network. It
is never an automatic fallback and cannot be requested above the worker's
configured policy. Do not enable it on a shared host or connect it to the Mac's
state. The general configuration default remains `read-only`; the dedicated
Daytona example explicitly selects `external-sandbox`. `workspace-write` keeps
inner controls for hosts that support them.

Claude's `workspace-write` mode uses `acceptEdits`, which passed a real file-edit
check. Other tool approvals can still be denied in noninteractive execution.

## Initial pilot qualification

The initial pilot built the image for Linux amd64. Its nonroot user ran both engine
binaries and the installed Flotilla CLI. The packaged supervisor waited for
provisioning and exited cleanly on SIGTERM.

A separate real Daytona sandbox running the source passed Claude and Codex
create, queued inputs, explicit-native-ID resume, duplicate-command handling,
reconnect/replay and exact-byte file transfer through an isolated loopback
control server. Claude also created a requested file with `workspace-write`.
Codex passed the selected `external-sandbox` policy: file editing, Python shell
execution, worker restart, exact-native-ID resume with conversation memory,
duplicate-intake handling, confirmed completion and exact-byte file transfer. These checks did not send Slack messages or
replace the production Mac client.
