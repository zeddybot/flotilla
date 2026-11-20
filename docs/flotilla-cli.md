# Flotilla CLI

`flotilla` operates the existing private server and Mac client on the **current host**. It does not administer a remote Daytona sandbox from your Mac. The `sc` command and the existing Slack app remain available.

For an existing installation, upgrade the server and client together: this client cannot connect to the older server protocol. Follow the [coordinated upgrade procedure](runtime-interface.md#coordinated-upgrade) before activating the new Mac service.

For a new personal profile, follow the [client pairing guide](connect-client.md).

## Bootstrap the first Mac with an operator-provisioned token

Start from this checkout with `uv` installed. Have the existing server URL, a shared client token file containing at least 32 characters with mode `600`, and your Conductor repository and workspace paths ready.

```sh
uv sync --frozen
uv run --frozen flotilla init --root "$HOME/.config/flotilla" \
  --server-url 'wss://<server-host>/client' \
  --token-file '/absolute/private/client.token' \
  --repo '/path/to/repository' \
  --workspace-root "$HOME/conductor/workspaces/ttd"
uv run --frozen flotilla --root "$HOME/.config/flotilla" config validate
scripts/install.sh --root "$HOME/.config/flotilla"
"$HOME/.local/bin/flotilla" --root "$HOME/.config/flotilla" status
```

Provision the token file before `init`; `init` validates it and creates `config.toml` without overwriting an existing file. For a local test server, `ws://localhost:<port>/client` is allowed. The installer reads config and token from the selected config directory and build files from the checkout. It builds a private runtime before switching the existing Mac LaunchAgent, retaining the label `com.sameer.slack-conductor` so a second client is not registered. `scripts/install.sh --root … --prepare-only` builds and validates without replacing the running client. `flotilla client install` and `flotilla client uninstall` provide the same service operations from a configured checkout or installed runtime.

The installer places a `flotilla` executable in `~/.local/bin` and prints the exact PATH instruction if that directory is absent from PATH. It never edits shell startup files. To make the command available in new bash or zsh shells, add `export PATH="$HOME/.local/bin:$PATH"` **once** to your `.bashrc` or `.zshrc` if it is not already there; repeated appends create duplicate entries. You can also run `~/.local/bin/flotilla` directly. Uninstall removes the LaunchAgent and managed wrapper but retains credentials, runtime files, and thread history.

Configuration lookup for commands other than `init` is: explicit `--root` (directory or `config.toml`), `FLOTILLA_CONFIG_DIR`, current directory, `~/.config/flotilla`, then the installed Mac runtime. Use `--root` when multiple configs exist. `init` defaults to `~/.config/flotilla`.

## Commands

| Command | Behavior |
| --- | --- |
| `init --server-url URL --token-file PATH --repo PATH --workspace-root PATH` | Create one Mac client config from existing credentials; no pairing wizard. |
| `config validate` | Validate config and shared token file. |
| `status`, `doctor`, `monitor [--watch] [--interval SECONDS]` | Show local health; `doctor` exits nonzero on unhealthy or unknown checks. `monitor` records local warning and recovery transitions in private `monitoring.json` and rotating `monitoring.log`. Watch interval defaults to 10 seconds and must be at least 5. |
| `client run`, `client install [--prepare-only]`, `client uninstall` | Run in the foreground, or manage the Mac user LaunchAgent. |
| `client start`, `client stop`, `client restart`, `client status`, `client logs [--lines N] [--follow]` | Operate the installed client on this Mac and read its local `daemon.log`. |
| `server run` | Run a server in the foreground on this host. |
| `server start`, `server stop`, `server restart`, `server status`, `server logs [--lines N] [--follow]` | On the managed `/data/config` server host, use the existing supervisor's `HOLD` file for control and read the local log. On an ordinary host, start/stop/restart are unsupported; `status` says unmanaged and `server run` is available. No arbitrary process is signaled. |
| `session list`, `session show ID`, `session tail ID [--after CURSOR] [--follow]` | Read local client sessions and events. |
| `session new PROMPT [--repo PATH] [--queue\|--steer] [--command-id ID]`, `session send ID PROMPT [--queue\|--steer] [--command-id ID]` | Create or send through the local Conductor adapter. The repository must match config. Command IDs can make retries durable. |
| `session archive ID [--command-id ID]` | Archive the session through Conductor. |
| `session open ID`, `session stop ID` | Parsed for capability discovery, but **unsupported** by the current Conductor adapter. Open the session or stop the agent in Conductor. |

Session commands require a client config and the local Conductor adapter. The current adapter reports reveal/open, interrupt/stop, and approval responses as unsupported capabilities. A running background job by itself is informational in monitoring.

By default, commands print readable text. `status` and `doctor` show a health summary and each check; `monitor` also shows incident transitions. Session commands show session IDs, details, and events with nested fields indented. Other structured command results use labeled fields. Text output is safe to display in a terminal and does not add color when redirected.

Use `--json` for scripts. It is a global option accepted before or after a subcommand. Its output shape is unchanged: one-shot success prints `{"ok":true,"data":…}`; failure prints `{"ok":false,"error":{"code":…,"message":…}}`. Exit codes are `0` for success, `1` for a command or health failure, `2` for usage, and `130` for interruption. `session tail --follow --json` and `monitor --watch --json` emit one JSON object per update. Foreground `run` and raw `logs --follow` reject `--json`; follow logs stream plain text.

This pilot does not include a public package, pairing, profiles, devices, or a setup wizard. Those designs are tracked separately in [the Flotilla design record](flotilla-design.md).

## Runtime controls and headless engines

```sh
flotilla session open SESSION_ID
flotilla session stop SESSION_ID
flotilla session show SESSION_ID
flotilla session respond SESSION_ID QUESTION_ID --answer 'selected answer'
flotilla session respond SESSION_ID QUESTION_ID --approve
flotilla session respond SESSION_ID QUESTION_ID --deny
flotilla session respond SESSION_ID QUESTION_ID --response-file reply.json
flotilla --root /data/config session new 'Inspect this repository' --engine claude --model haiku
flotilla --root /data/config session new 'Inspect this repository' --engine codex
```

`session show` exposes pending request IDs when native discovery is available.
Repeat `--answer` for multiple questions. Plan approvals use the discovered request
kind. Structured Codex permission grants and MCP form responses require
`--response-file` with explicit native response fields; they are never inferred
from a generic approval. Conductor stop returns `requested` until observation
confirms rest and pauses its queue. Resume that queue in Conductor when appropriate.

`--engine` applies only to a headless worker configuration. Headless commands
contact the running worker service over a private local socket. Its default send
mode is queue, and `--steer`, interactive open, and native question replies are
unsupported. `session tail` drains bounded replay pages even without `--follow`.
