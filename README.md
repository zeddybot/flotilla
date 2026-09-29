# Conductor for Slack

The current private, single-owner deployment runs the Slack server on Daytona and a native Conductor client on the Mac. It uses the existing **Conductor** Slack app and keeps the same thread, queue, steer, notification, and file behavior. The Mac connects outbound to the server; it still runs Conductor and the agent. See the [client/server deployment and rollback guide](docs/client-server-deployment.md) and [validation record](docs/client-server-validation.md). The broader **Flotilla** multiplayer and plugin design remains a [future plan](docs/flotilla-design.md).

A personal Slack app that drives the local Conductor Mac app. Mention **@Conductor** or DM it to create a TTD workspace. Mention **@Conductor** on every channel follow-up to continue the same session; ordinary thread conversation is ignored. Direct messages do not require a mention. Agent results replace a status message in the thread. You can open and continue the same workspace in Conductor.

The bot always sends the configured owner a fresh DM when a reply reaches **Done**, **Failed**, or **Needs attention** (including questions and permission/plan requests). Each DM contains the task and a link to the original reply. Completion waits for background work and final files/pages; superseded steers and historical completed replies do not generate completion alerts. Follow up in the original thread; replies to notification DMs show a link back and do not create an agent. There is no notification preference or opt-out command.

Notification delivery is persisted separately from answer delivery. After a lost send response, the bridge checks DM history for the same notification before retrying. If the very first DM response is lost before the bridge knows the DM channel, sending a message to the bot supplies that identity and allows reconciliation. Slack desktop/mobile alerts still follow your Slack notification and Do Not Disturb settings.

Only the configured Slack owner can issue commands. The Daytona server owns Slack credentials and delivery state; the Mac client reads Conductor's SQLite database in read-only mode and calls its native services for agent actions. macOS Accessibility is used only to attach the native dispatcher. The bridge never writes Conductor's database. Ordinary messages typed in Conductor are not relayed.

## Install

The server and Mac client are the only runtime modes. Follow the
[deployment guide](docs/client-server-deployment.md) to provision Daytona.
For the host-local `flotilla` command, separate config directory, and service
operations, see the [Flotilla CLI guide](docs/flotilla-cli.md).
The server owns the Slack bot/app tokens; the Mac holds only its client token
and existing Conductor credentials. For development, both processes can run on
the same machine with `ws://localhost:8765/client` and separate state directories.

The Mac needs Conductor and [uv](https://docs.astral.sh/uv/). Copy
`config.example.toml` to `config.toml`, review the repository/workspace paths,
and set the server's actual preview address and a mode-600 token file:

```toml
mode = "client"
server_url = "wss://<preview-host>/client"
remote_token_file = "/absolute/private/path/client.token"
```

Install or update the Mac client:

```sh
uv sync --frozen
uv run python scripts/install_service.py
uv run sc doctor
```

The installer builds a private runtime behind
`~/Library/Application Support/slack-conductor/runtime`, installs locked
production dependencies, and registers
`~/Library/LaunchAgents/com.sameer.slack-conductor.plist`. It always installs
client mode and does not copy a Slack `.env` into the new runtime. Rerun it after
client code, configuration, or token changes. It does not edit shell startup
files or add `sc` to PATH; it creates a `flotilla` wrapper in `~/.local/bin`.

The client starts at user login, restarts after failure, and needs a logged-in
desktop session. Daytona can receive requests while the Mac is offline;
execution resumes when the client reconnects. If Accessibility access is denied,
allow the runtime's Python interpreter under System Settings → Privacy & Security
→ Accessibility. Inspect the installed service's log as well as `sc doctor`.

```sh
launchctl print "gui/$(id -u)/com.sameer.slack-conductor"
tail -n 40 "$HOME/Library/Application Support/slack-conductor/daemon.log"
uv run python scripts/install_service.py --uninstall
```

Uninstall stops automatic startup but preserves credentials, runtime files,
and state. `--prepare-only` builds a separate runtime and plist without
replacing the running client. There is no `--mode` installer flag.

For a new Slack app installation, use `slack-manifest.json` and put the variables
from `.env.example` in the server's private `.env`, never on the Mac client.
The existing Conductor Slack app requires no reinstall for this runtime change.

## Use

Invite **Conductor** to the channel first, then:

```text
@Conductor [opus][high] investigate this bug
@Conductor [queue] also add a focused regression test
@Conductor [steer] stop that approach and inspect the API contract first
@Conductor [astra][max][fast] continue with this model
@Conductor [verbose] include tool activity in the thread
@Conductor [close]
@Conductor [new] start a fresh workspace for the next task
```

The defaults are Fable 5.1, high effort, and steering. A follow-up while the agent is working goes into its running turn; `[queue]` waits and starts a separate turn afterward. An idle agent starts either kind immediately. Model tokens include `[astra]` → GPT-6 Astra, `[sol]` → GPT-6 Sol, `[terra]` → GPT-5.6 Terra, `[luna]` → GPT-6 Luna, `[sonnet]` → Sonnet 5 1M, and `[alpha]` → OpenCode Zen's Union Alpha Free. Alpha uses Conductor's OpenCode integration through ACP, independent of picker favorites; effort uses the provider default and fast mode is unavailable. Add `alpha = "Union Alpha Free"` to `[models]` in an existing local `config.toml` to enable the alias there.

The prompt encourages agents to @mention people they address directly or need action from, using real Slack user IDs from the thread or Slack tools. The relevant person may be someone other than the dispatcher.

The tokens must lead the prompt, after an optional bot mention. Model, effort, and fast mode are configured when a session starts; use `[new]` to change them. Verbose and send mode are sticky in each Slack thread: after `[queue]`, use `[steer]` to return that thread to steering. Model/effort/fast selection is best effort; mismatches appear in the status. Unknown bracket tokens remain prompt text. `[close]` archives the mapped workspace and may stop its running agent. `[new]` maps the thread to a new workspace; any older active turn still delivers its own result.

The bridge calls Conductor’s native services for creation, configuration, sending, queueing, steering, and archiving. This is the only control path. It binds each Slack thread to a workspace and session independently of the selected chat, so you can continue typing and navigating in Conductor. Inspector opens briefly during startup/reconnection to attach the native dispatcher, then closes. Accessibility permission is needed only for that bootstrap; agent prompts never pass through the UI composer.

## Recovery and diagnostics

Every message has an emoji/status header, optional detail, and optional agent content:

```text
⏳ Waiting · preview deployment · 1 background job

The preview is building. I'll verify the fix after it deploys.
```

The header follows Creating workspace, Starting, Thinking, Working, Waiting, Queued, Done, Needs attention, Failed, Reconnecting, Archiving, or Archived. Header changes preserve the latest agent update. Verbose tool messages and long-answer continuations use the same format.

A result with outstanding background jobs is an interim update. The bridge tracks job completion/failure/cancellation and the agent's resumed response, including across restarts. Done requires a result, no outstanding tracked jobs or pending callback, and two seconds at rest. Job failure alone does not mean the agent has failed. A subsequent queued turn may run while the previous turn's final answer is delivered.

After steering pickup is confirmed, the run's status, content, and background-job tracking move into the new acknowledgment below your steering message. The older response is deleted, so the final answer and any continuations appear below the steer. Queued follow-ups keep the previous run's message through Done and get separate messages. Needs attention reports actionable bridge failures and explicit agent question events; blocked states are not inferred from ordinary prose. The native runner also reconciles tracked jobs after a crash: jobs missing from the recovered runner no longer keep Slack waiting. A resumed agent is Working until its new result; an agent that has stopped without a new result needs attention.

This handoff also applies when steering a run that is waiting on background work and Conductor assigns the follow-up a new native turn ID. The bridge checks job/callback state immediately before that follow-up, then transfers ownership before processing its output. A previous run that already finished its work keeps its separate reply, even if its Done update has not reached Slack yet.

In the split deployment, Daytona's private state directory contains `bridge.sqlite`, `remote.sqlite`, upload snapshots, `daemon.lock`, and `daemon.log`. The Mac retains `native.sqlite` (commands and pause intent), `native-heartbeat.json`, `native-attachment.json` (the loopback address and token of the installed frontend), its client log, and launchd startup logs. Treat all logs and state as private because prompts, tool descriptions, and results can contain sensitive information.

Incoming Slack messages are durably deduplicated by workspace/channel/message timestamp, including overlapping mention and message events. Restart recovery resumes queued/running turns and partially delivered results. Native sends have stable IDs and a durable command journal. Recovery checks both accepted messages and the native outbox before resubmitting an interrupted operation; workspace creation also reuses its original identity. Slack and local SQLite cannot commit atomically: deterministic Slack message IDs reduce duplicate posts, but this is not an exactly-once transport.

A heartbeat watchdog declares the Socket Mode connection lost after 120 seconds without a new pong, after a sleep of 30 seconds or more, or after five transport errors. Repeated socket creation does not reset the pong deadline. The Slack service then replaces the connection in-process, with backoff. The server supervisor restarts the process if the service loop cannot replace the connection within 60 seconds or no connection stays stable for 3 minutes. The split deployment keeps the Mac's native attachment separate from Slack reconnection. Persisted turns resume either way. Messages never received during a Slack outage are not automatically recovered from channel history.

Agent content uses Slack's native [Markdown blocks](https://docs.slack.dev/reference/block-kit/blocks/markdown-block/): headings, bold/italics (including nested emphasis), strikethrough, links, ordered/unordered lists, task lists, block quotes, dividers, tables, inline code, and fenced code blocks. All heading levels display at the same size; images become links. Code language tags enable syntax highlighting. The live status header stays above the formatted content, including on edits and continuations. Notification and accessibility fallback text is retained. If Slack rejects a Markdown payload (for example, more than 50 rendered blocks), the full text is delivered using Slack's basic text formatting instead.

Replies split at Markdown boundaries to respect both message length and Slack's rendered-block limit. Headings and ordinary paragraphs stay intact; long lists split between items, and tables split between rows with repeated column headers (up to 99 data rows per table). Oversized paragraphs reopen emphasis, links, and inline code on continuation pages. Long fenced code blocks retain their language tag and split between lines when possible. Each continuation retains its status header and uses the existing resumable delivery bookkeeping. The text fallback above is a last resort for Markdown Slack still rejects, rather than the normal way of handling long or heading-heavy replies. An individual table row or URL larger than a message may not retain its original formatting. Status updates are coalesced to at most one per 1.5 seconds per turn; rate-limit responses honor Slack's retry delay.

Final replies can include files using ordinary Markdown anywhere in the answer:

~~~markdown
Fixed the dashboard:

![Updated dashboard](.context/dashboard.png)

Download the [results](.context/results.csv).
~~~

Relative paths resolve against the mapped Conductor workspace; absolute paths and local
file URLs also work. Images using PNG, JPEG, or GIF render inline, and other files get
downloadable Slack links and attachments on the same reply. Web links, source locations
such as app.py:42 or app.py#L42, and examples inside code blocks stay unchanged.
Only link local files intended for everyone in the Slack conversation.

The bridge waits for the final answer and all tracked background work, snapshots the
selected files on the Mac, transfers them to the server, and shows **📤 Sending · N files** while uploading. Steering follows the
replacement reply; queued requests retain separate files. Upload IDs and progress survive
restarts. Missing files or uploads that fail after bounded retries preserve the answer
and show Needs attention with a reason beside each unavailable file. Limits are 10 distinct
files per reply and 25 MiB per file; unsupported image formats remain downloadable files.
Snapshots are removed after delivery. The app needs **files:write** for uploads and
**files:read** to recover ambiguous finalization and retrieve file links; reinstall the
Slack app after adding these scopes to an existing installation.

## CLI

```text
sc [--root CONFIG_DIRECTORY] doctor
sc [--root CONFIG_DIRECTORY] server
sc [--root CONFIG_DIRECTORY] client
sc [--root CONFIG_DIRECTORY] new REPO PROMPT
sc [--root CONFIG_DIRECTORY] send WORKSPACE PROMPT [--queue | --steer]
sc [--root CONFIG_DIRECTORY] archive WORKSPACE
sc [--root CONFIG_DIRECTORY] tail SESSION
```

`--root` precedes the command and defaults to the current directory. `doctor`
uses that directory's configured role: server health or Mac attachment/link
health. `server` and `client` start foreground services; launchd already runs
the installed Mac client. There is no `daemon` command or `mode = "local"`.

`new`, `send`, `archive`, and `tail` require a Mac client configuration. Actions
use the running native journal; they do not create Slack threads. `new` requires
the configured repository and prints workspace/session IDs. `send` defaults to
the configured send mode; model/effort defaults come from configuration. `tail`
reads the native transcript until interrupted. There are no nested management
subcommands or model-selection CLI flags yet.

To use the installed runtime from any directory, add this function to your
zsh/bash startup file:

```sh
sc() {
  local runtime="$HOME/Library/Application Support/slack-conductor/runtime"
  "$runtime/.venv/bin/sc" --root "$runtime" "$@"
}
```

See [the validation record](docs/client-server-validation.md) for test and live deployment evidence. Design and original constraints: [SPEC.md](SPEC.md).

## Native recovery

Queue pause/resume intent is written durably before the native action is acknowledged. Reattachment restores that intent, so a newer stop wins over an older send retry. While the daemon is away the frontend applies your actions directly and reports the latest one per session when a daemon returns. A pre-existing queue with unknown intent is left paused. A new explicit send follows Conductor’s normal resume behavior. Slack shows a paused queued message as Needs attention.

Quitting Conductor may kill its background shell jobs; the bridge does not rerun their commands automatically. It reconciles the live runner and transcript and updates Slack accordingly. Recovering lost plan-mode approval callbacks is out of scope.

The native integration uses Conductor’s internal frontend services and validates them at attachment. App updates are allowed; an incompatible interface reports a reconnect/attachment error instead of switching to UI automation. See [the implementation and validation record](docs/conductor-native-bridge.md).

`sc new`, `sc send`, and `sc archive` submit native commands through the running daemon. `sc doctor` checks its native attachment. Install/start the LaunchAgent before using those commands. The installer replaces the runtime source tree exactly, removing retired modules; `--prepare-only` builds a separate runtime without modifying the running service.

## Development

See [Testing and merge protection](docs/testing.md) for the component boundaries,
behavioral coverage, focused local checks, and required-CI configuration.

Planned work is tracked in [next steps](docs/next-steps.md).
