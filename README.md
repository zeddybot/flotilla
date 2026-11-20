# Flotilla

![Flotilla's three blue boats](assets/flotilla/logo.svg)

Mention **@flotilla** in Slack or send it a DM to start an agent in your configured repository. In a channel, mention **@flotilla** on each follow-up to continue the thread’s session. Flotilla edits its status message as the agent works and delivers the answer and files in that thread.

With the Conductor client, the agent runs on your Mac. With a headless client, it runs Claude or Codex in its configured repository on Daytona. Flotilla routes each new Slack thread to the first summoner’s default or named profile. Registered teammates can prompt the same agent. Its owner controls approvals, interruption and session replacement.

## Install

Follow the [client setup guide](docs/connect-client.md) to pair your Mac or Daytona worker. The [server deployment guide](docs/client-server-deployment.md) covers hosting, migration and rollback. The server holds the Slack credentials. Clients connect outbound and keep their own agent sign-ins.

For a new Slack app installation, use `slack-manifest.json` and put the variables
from `.env.example` in the server's private `.env`, never on the Mac client.
An existing installation can use the same Slack app and bot user ID. Update its display name and bot name to match the manifest, then upload the [Flotilla app icon](assets/flotilla/slack-icon.png).

## Use

Invite **Flotilla** to the channel and mention it in a message.

```text
@flotilla [fable][high] investigate this bug
@flotilla [queue] also add a focused regression test
@flotilla [steer] inspect the API contract first
@flotilla [new][astra][max][fast] start a session with this model
@flotilla [verbose] include tool activity in the thread
@flotilla [close]
@flotilla [new] start a fresh workspace for the next task
```

On a fresh Conductor server the defaults are Fable 5.2, high effort, and steering. A follow-up while the agent is working goes into its running turn; `[queue]` waits and starts a separate turn afterward. An idle agent starts either kind immediately. Model tokens include `[fable]` → Fable 5.2, `[haiku]` → Haiku 5.5 (1M), `[astra]` → GPT-6.2 Astra, `[sol]` → GPT-6.2 Sol, `[terra]` → GPT-5.6 Terra, `[luna]` → GPT-6 Luna, `[sonnet]` → Sonnet 5.5, and `[alpha]` → OpenCode Zen's Union Alpha Free. Fable 5.2 supports `[fast]` and effort through `[ultracode]`; Haiku 5.5 supports effort through `[max]` and standard speed. Alpha uses Conductor's OpenCode integration through ACP, independent of picker favorites; effort uses the provider default and fast mode is unavailable. Existing installations with a `[models]` table in `config.toml` must update their aliases there; the table overrides these defaults. Model selection applies when a session starts; use `[new][fable]` or `[new][haiku]` to select the new models from an existing thread. Explicit Fable 5.1 and Haiku 4.5 labels remain supported for existing sessions and custom aliases.

On a headless profile, start a Codex session with `[profile:daytona][sol]` or a Claude session with `[profile:daytona][opus]`. Both accept `[steer]` while running. Choose the default repo, model and follow-up mode in the [client configuration](docs/connect-client.md#configure-a-client).

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

A heartbeat watchdog declares the Socket Mode connection lost after 120 seconds without a new pong, after a sleep of 30 seconds or more, or after five transport errors. Repeated socket creation does not reset the pong deadline. The Slack service then replaces the connection in-process, with backoff. The server supervisor restarts the process if the service loop cannot replace the connection within 60 seconds or no connection stays stable for 3 minutes. The split deployment keeps the Mac's native attachment separate from Slack reconnection. Persisted turns resume either way.

The server also checks the owner's DM history every minute for missing top-level requests from the last five minutes, allowing ten seconds for normal event delivery. Recovery uses the same durable message identity as Socket Mode, so overlapping delivery and restarts cannot dispatch a second copy. It skips edits, thread replies, and other users; channel mentions and longer outages are not backfilled. History API failures or stale scans appear as `intake_recovery` warnings in server monitoring. Intake logs record message/event IDs and acceptance or rejection reasons without prompts or file URLs. A healthy socket alone does not prove a particular message was received.

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
