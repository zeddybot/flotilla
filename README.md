# Conductor for Slack

A personal Slack app that drives the local Conductor Mac app. Mention **@Conductor** or DM it to create a TTD workspace. Replies in the same Slack thread continue that session; agent results replace a status message in the thread. You can open and continue the same workspace in Conductor.

Only the configured Slack owner can issue commands. The bridge reads Conductor's SQLite database in read-only mode and uses macOS Accessibility for all actions. It never writes Conductor's database. Ordinary messages typed in Conductor are not relayed.

## Install

Requires macOS, Conductor, and [uv](https://docs.astral.sh/uv/). The adapter uses Python 3.12.

```sh
uv sync --frozen
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env
```

Review the repository and workspace paths in `config.toml`. The default target is `/Users/sameer/Documents/work/prodex/ttd`. Model labels are configurable because Conductor's picker can change.

Create an app named **Conductor** at [Slack's app dashboard](https://api.slack.com/apps) using `slack-manifest.json`, install it in ProDex Labs, and generate an app-level token with `connections:write`. Put the following in `.env`:

```dotenv
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-socket-mode-token
OWNER_SLACK_USER_ID=your-slack-user-id
SLACK_TEAM_ID=your-slack-team-id
```

`.env` and local `config.toml` are gitignored. Credentials must have mode `600`; the daemon refuses group/world-readable `.env` files. Tokens are never written into the launchd plist. This uses [Bolt Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/) without a public HTTP endpoint. Channel events are subscribed in addition to mentions so unmentioned replies in a mapped thread work.

Verify the adapter, then install the daemon:

```sh
uv run sc doctor
uv run python scripts/install_service.py
```

The installer copies a private runtime (including a mode-600 credential copy) into `~/Library/Application Support/slack-conductor/runtime`, installs locked production dependencies, and registers `~/Library/LaunchAgents/com.sameer.slack-conductor.plist`. This keeps the service independent of the development worktree. Rerun the installer after changing code, configuration, or credentials.

The daemon starts **at user login after boot**, restarts after failure, and needs a logged-in desktop session to control Conductor. It cannot control the UI before login or while the Mac is asleep. If Accessibility access is denied, allow the runtime's Python interpreter under System Settings → Privacy & Security → Accessibility. `sc doctor` from a Conductor terminal does not prove the separate launchd process has permission; inspect the daemon log after installation.

```sh
launchctl print "gui/$(id -u)/com.sameer.slack-conductor"
tail -n 40 "$HOME/Library/Application Support/slack-conductor/daemon.log"
uv run python scripts/install_service.py --uninstall
```

Uninstall stops automatic startup but preserves runtime files, credentials, and conversation history. App tokens can be revoked in Slack's app dashboard. `--prepare-only` prepares the runtime and plist without starting the service.

## Use

Invite **Conductor** to the channel first, then:

```text
@Conductor [opus][high] investigate this bug
[queue] also add a focused regression test
[steer] stop that approach and inspect the API contract first
[astra][max][fast] continue with this model
[verbose] include tool activity in the thread
[close]
[new] start a fresh workspace for the next task
```

The defaults are Fable 5.1, high effort, and steering. A follow-up while the agent is working goes into its running turn; `[queue]` waits and starts a separate turn afterward. An idle agent starts either kind immediately. The model tokens select Conductor's current picker labels: `[astra]` → GPT-6 Astra, `[sol]` → 5.6 Sol, `[terra]` → 5.6 Terra, `[luna]` → 5.6 Luna, and `[sonnet]` → Sonnet 5 1M.

The tokens must lead the prompt, after an optional bot mention. Model, effort, fast, verbose, and send mode are sticky in each Slack thread: after `[queue]`, use `[steer]` to return that thread to steering. Model/effort/fast selection is best effort; mismatches appear in the status. Unknown bracket tokens remain prompt text. `[close]` archives the mapped workspace and may stop its running agent. `[new]` maps the thread to a new workspace; any older active turn still delivers its own result.

The bridge serializes UI actions across all threads, refuses to overwrite unsent drafts, checks the target workspace and active session, and uses a unique Slack header to correlate each message with its agent turn. Keep the app available; AeroSpace recovery can briefly switch desktops. Avoid navigating or editing Conductor during the brief send operation.

## Recovery and diagnostics

The private state directory contains `bridge.sqlite`, `daemon.lock`, rotating `daemon.log`, launchd startup logs, and the latest selector-failure snapshot. The snapshot omits AX values; treat all local logs and state as private because prompts, tool descriptions, and results can contain sensitive information.

Incoming Slack messages are durably deduplicated by workspace/channel/message timestamp, including overlapping mention and message events. Restart recovery resumes queued/running turns and partially delivered results. It never blindly replays an interrupted UI send. If delivery is ambiguous, check Conductor before retrying. Slack and local SQLite cannot commit atomically: deterministic Slack message IDs reduce duplicate posts, but this is not an exactly-once transport.

Normal fenced code blocks stay together. A code block that itself exceeds 3,900 characters is split into separately fenced continuations so every Slack message remains bounded. Status updates are coalesced to at most one per 1.5 seconds per turn; rate-limit responses honor Slack's retry delay.

Adapter-only commands:

```sh
uv run sc new /Users/sameer/Documents/work/prodex/ttd 'your prompt'
uv run sc send WORKSPACE_ID 'follow-up' --queue
uv run sc send WORKSPACE_ID 'steering message' --steer
uv run sc tail SESSION_ID
uv run sc archive WORKSPACE_ID
uv run sc daemon
```

These commands act on real Conductor workspaces. Do not run the action commands concurrently with the daemon. The daemon takes an exclusive lock to prevent duplicate service instances.

Targeted tests: `uv run pytest tests/test_db.py tests/test_bridge.py -q`. CI checks the bridge tests and Python formatting. Live macOS Accessibility and Slack installation require local verification. Design and original constraints: [SPEC.md](SPEC.md).
