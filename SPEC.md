# slack-conductor

A personal Slack bot that drives the **local** Conductor Mac app. `@bot` in a Slack channel or DM creates a Conductor workspace and starts an agent turn; replies in the thread continue that agent; the agent's output comes back to the thread; the session stays a normal Conductor session you can take over in the app.

Everything below marked *verified* was exercised against Conductor 0.84.2 / Claude Code 2.1.257 on 2026-09-07.

## Scope

In:
- Slack → Conductor: user messages only (new workspace, follow-up, queued follow-up, archive).
- Conductor → Slack: agent output only. Turns you type in the Conductor UI are not relayed.
- Per-thread model, effort, fast mode, send mode via bracket tokens.
- One edited status message per turn.

Out:
- Cloud workspaces (Conductor's public API is cloud-only; irrelevant here).
- Relaying your own Conductor-side messages to Slack.
- Multi-user. The bot answers exactly one Slack user id.

## Why this shape (verified constraints)

| Surface | Finding |
|---|---|
| Conductor public API / CLI / MCP | Cloud workspaces only. Cannot create or message a local workspace. |
| `conductor://prompt=<enc>&path=<abs repo>` | Creates a real local workspace (worktree under `~/conductor/workspaces/<repo>/<city>`, branch `<prefix>/<city>`, one session with the default model) and **prefills** the composer. Nothing is sent. |
| `osascript` / System Events | Cannot see Conductor's window (`Can't get window 1 of process "conductor" (-1719)`). |
| Raw Accessibility API (`ApplicationServices` via pyobjc) | Full DOM of the WKWebView is exposed. Set composer value, press buttons, open menus, all without Conductor being frontmost. |
| `conductor.db` (SQLite, WAL) | Readable live while the app runs. Every stream message of a turn lands in `session_messages` within ~1 s. Never written by this project. |
| Conductor "Follow-up behavior" | User setting `default_message_send_mode` ∈ `steering` (inject mid-turn, agent may ignore) or `queued` (own turn after the current one). Cmd+Enter flips to the non-default mode. |
| AeroSpace | Hides inactive-workspace windows off-screen; once off-screen the AX tree drops the window. `aerospace workspace <n>` brings it back; a brief focus steal is accepted. |

## Architecture

```
Slack (Socket Mode) ──app_mention / message.im──▶ bridge (Python, launchd, this Mac)
     ▲                                               │
     │ chat.postMessage / chat.update / chat.delete    │ conductor adapter
     │                                                 ├─ deep link (new workspace)
     │                                                 ├─ AX: navigate · compose · send / Cmd+Enter · model · effort · archive
     └──────────── poller ◀── conductor.db (read-only) ┘
                                                        thread map: bridge.sqlite
```

Stack: Python 3.12 via `uv`; `slack-bolt` (Socket Mode); `pyobjc-framework-ApplicationServices`, `-Cocoa`, `-Quartz`; stdlib `sqlite3`. Python is chosen because the AX layer is already proven in pyobjc; a Bun/TS bridge would need a Swift helper for the same calls.

## Slack app

- Socket Mode. App-level token with `connections:write`. No public URL.
- Bot scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `users:read`.
- Events: `app_mention`, `message.im`.
- Every event is dropped unless `event.user == OWNER_SLACK_USER_ID` and the sender is not a bot. Channel text is untrusted input to the agent.
- Replies via `chat.postMessage` with `thread_ts`; status edits via `chat.update`; superseded status via `chat.delete`; permalink via `chat.getPermalink`. Slack's streaming API is not used (thread replies unsupported in ordinary channels).
- Message text > 3900 chars is split into consecutive thread replies. Code fences are not split mid-block.

### Token grammar

Leading bracket tokens on the mention text, any order, case-insensitive. Sticky per thread once set.

| Token | Effect |
|---|---|
| `[fable]` `[opus]` `[sonnet]` `[haiku]` `[astra]` `[sol]` `[terra]` `[luna]` | Model, resolved through `config.models` to a Conductor picker label (e.g. `Fable 5.1`, `GPT-6 Astra`). |
| `[low]` `[medium]` `[high]` `[xhigh]` `[max]` `[ultracode]` | Effort, verified against `sessions.claude_effort_level`. |
| `[fast]` | Toggle fast mode on. |
| `[queue]` / `[steer]` | Sticky send mode when the agent is busy. Default `steer`. |
| `[new]` | In a thread that already maps to a session: create a new workspace instead of following up. |
| `[close]` | Archive the thread's workspace. |
| `[verbose]` | Also post one line per tool call instead of only editing the status. |

Defaults live in `config.toml`: `model = "Fable 5.1"`, `effort = "high"`. The repo is fixed: every workspace is created in `/Users/sameer/Documents/work/prodex/ttd`; there is no per-message repo selection.

Model and effort are **best effort**: the bridge tries to set them, verifies from `sessions`, and if Conductor ends up elsewhere it says so in the status message and proceeds. A wrong model never blocks a turn.

### Prompt header

Prepended to every message sent to the agent:

```
[slack] channel=<C…> thread_ts=<ts> message_ts=<ts> permalink=<url>
You already have Slack tools; read the thread or channel yourself if you need more context.
The bridge relays your final reply to this thread. Do not post to Slack yourself.

<user text with the bot mention and tokens stripped>
```

## Conductor adapter

### Process and window

- pid: the process whose `comm` is `/Applications/Conductor.app/Contents/MacOS/conductor`. `pgrep -x conductor` does not match; `pgrep -f MacOS/conductor` can match helpers.
- Window: `AXWindows[0]`; if empty, `AXMainWindow`; if that is `None`, the window is off-screen. Recovery:
  ```
  ws=$(aerospace list-windows --app-bundle-id com.conductor.app --format '%{workspace}')
  aerospace workspace "$ws"          # focus steal accepted
  ... AX actions ...
  aerospace workspace-back-and-forth
  ```
  `open -a Conductor` also works but is slower to return the user. Never call either when the window is already reachable.

### Selectors (verified)

| Target | Selector |
|---|---|
| Sidebar workspace | `AXLink` whose `AXURL` = `tauri://localhost/repository/<repo_id>/workspace/<workspace_id>`. Match on `workspace_id`; titles get LLM-renamed within a minute. |
| Composer | `AXGroup` with `AXDescription == "composer"` |
| Editor | `AXTextArea` inside composer (TipTap). `AXValue` is settable and reads back. `AXFocused` is settable. |
| Send | `AXButton` title starts with `Send message` |
| Model | `AXPopUpButton` title starts with `Change agent (`; AXPress opens `AXMenuItem`s titled `"<Model>\nSet <Model> as default and select N"`. AXPress the item to select and close. Escape does not close the menu. |
| Effort | `AXButton` whose title is a level. It is a **cycle button**: each press advances low → medium → high → xhigh → max → ultracode → low. Read `sessions.claude_effort_level` after each press; stop at the target. |
| Fast | `AXButton "Fast"` (toggle) |
| Busy indicator | `AXGroup` with description `Working...` in the chat pane |
| Queue list | `AXStaticText "Queued"` followed by items whose value is `"<text> Edit Delete Steer"` |
| Archive | `AXShowMenu` on the sidebar link → `AXMenuItem` containing `Archive` → confirm `AXButton "Archive workspace"`. While an agent is running the confirm is `"Stop agents and archive ⌘ Enter"` and the sidebar is not queryable until the dialog closes. |
| New workspace | `AXButton "New workspace"` (unused; the deep link is simpler and carries `path`). |

Send mode override: set `AXFocused = True` on the editor, then post Cmd+Enter to the pid:
```python
for down in (True, False):
    ev = CGEventCreateKeyboardEvent(None, 36, down)
    CGEventSetFlags(ev, kCGEventFlagMaskCommand)
    CGEventPostToPid(pid, ev)
```
Verified: composer clears, message appears under "Queued", is absent from `session_messages` until the running turn ends, then runs as its own turn.

Every selector lookup fails loudly with the tree snapshot attached. A missing label means Conductor changed its UI; the bridge posts "Conductor UI changed: <selector>" to the thread and stops that action. Nothing is retried blindly.

### Database (read-only)

Open `file:~/Library/Application Support/com.conductor.app/conductor.db?mode=ro`.

```sql
-- new workspace after a deep link
select id, active_session_id, directory_name, workspace_path
  from workspaces where created_at > :t order by created_at desc limit 1;

-- turn state
select status from sessions where id = :session;             -- idle | working

-- stream since last seen (assistant rows are JSON; user rows are plain text)
select id, role, content, sent_at, turn_id, created_at
  from session_messages
 where session_id = :session and created_at > :last_seen
 order by created_at;
```

Row types the poller acts on (`json_valid(content)` guard first):

| `type` / `subtype` / `message.content[0].type` | Status text |
|---|---|
| `system/init` | `starting agent` |
| `assistant/…/thinking` | `thinking` |
| `system/task_started` | `description` field verbatim (Bash calls get this) |
| `assistant/…/tool_use` | `<name>: <first string arg, 60 chars>` |
| `assistant/…/text` | ignored until result |
| `result` | final; post `result` text; `is_error` → prefix `error:` |
| `session_state_changed`, `task_notification`, `background_tasks_changed`, `task_updated`, `user/tool_result` | ignored |

Workspace lifecycle: `workspaces.state` `ready` → `archived` (worktree removed within ~2 s, `archive_commit` set).

## Flows

### New request (top-level mention, or `[new]`)
1. Reply in thread: `creating workspace`. Record `status_ts`.
2. `open "conductor://prompt=<urlenc text>&path=<repo>"`. Poll `workspaces` (≤10 s) for the new row; store `workspace_id`, `session_id`.
3. Ensure window; AXPress the sidebar link for `workspace_id`.
4. Model: if the picker title ≠ target, open and AXPress the item. Effort: cycle to target, at most 8 presses. Fast: toggle if requested. Read all three back from `sessions`; on mismatch append `(model: <actual>, effort: <actual>)` to the status message and continue.
5. Set editor value = header + text (the deep link's prefill is replaced, not appended). AXPress Send.
6. Status → `starting agent`; poller takes over (see below).

### Follow-up in a mapped thread, agent idle
Ensure window, navigate, set editor, Send. Status message as above.

### Follow-up in a mapped thread, agent working
1. Reply: `enqueuing message`.
2. Navigate, set editor. If effective mode is `queue` and the user default is `steering` (or vice versa), Cmd+Enter; else Send.
3. Queue mode: confirm the text under "Queued" in the tree → status `queued`. Pickup = a new `user` row with this text and a fresh `turn_id`. Steer mode: pickup = the `user` row appears immediately (same `turn_id`); status `steering`.
4. On pickup: delete the previous turn's status message, this one becomes live.
5. Multiple queued follow-ups: one status message each, matched to their `user` row by text and order.

### Poller (per active turn)
- Tick every 1 s. Coalesce status edits to ≤1 per 1.5 s.
- On `result`: replace the status message text with the result; if > 3900 chars, status gets the first chunk and the rest follow as replies. Turn ends when `sessions.status` reads `idle` and no queued item remains.
- If the thread's workspace hits `archived` mid-turn, post `workspace archived in Conductor` and stop.

### `[close]`
Ensure window, sidebar link → context menu → Archive → confirm (`Archive workspace`, or `Stop agents and archive` if working). Wait for `state = archived`. Reply with `archived <archive_commit[:10]>`. Mark the thread `archived`; further mentions in that thread get "workspace archived; use [new]".

### Startup
Reconcile every `running` thread against `sessions.status`; finish or fail their status messages. Verify the AX selectors against a live window once and refuse to start if any is missing.

## Data model (`bridge.sqlite`)

```sql
create table threads (
  channel        text not null,
  thread_ts      text not null,
  workspace_id   text not null,
  session_id     text not null,
  model          text, effort text, fast integer default 0, send_mode text default 'queue',
  verbose        integer default 0,
  state          text not null,            -- creating | idle | running | archived | broken
  last_row_ts    text,                     -- session_messages.created_at high-water mark
  primary key (channel, thread_ts)
);
create table status_messages (
  channel text, thread_ts text, status_ts text primary key,
  user_text text, turn_id text, kind text  -- turn | queued | close
);
```

Everything else is read from `conductor.db` at the moment of use.

## Failure handling

| Failure | Behavior |
|---|---|
| No Conductor window | AeroSpace switch; if still none after 5 s, post `Conductor not reachable` and leave the thread `idle` for retry on next mention. |
| Selector missing | Post the selector name, attach tree snapshot to the log, abort the action. |
| Deep link produced no row in 10 s | Post `workspace creation failed`, no retry (a second link would make a second workspace). |
| Model/effort mismatch | Best effort: note the actual values in the status message and continue. Never block a turn on it. |
| `result.is_error` | Post as error, keep the last status line. |
| Bridge restart mid-turn | Startup reconciliation above. |
| Slack rate limit (429) | Honor `Retry-After`; status edits are already coalesced. |

## Security

- Owner-only: exact Slack user id match, bots ignored. Anyone else mentioning the bot gets silence.
- Tokens in `~/.config/slack-conductor/.env`, mode 600; never in the repo.
- Prompt header is fixed text; user text is appended verbatim. No channel history is copied into the prompt; the agent fetches it with its own Slack tools if needed.
- `conductor.db` opened read-only. The bridge never writes Conductor state.
- Accessibility permission is granted to the launchd-run interpreter once.

## Repository layout (planned)

```
slack-conductor/
  SPEC.md
  README.md
  pyproject.toml
  config.example.toml
  src/slack_conductor/
    __main__.py        # launchd entry
    slack_app.py       # Bolt handlers, token parsing, chunking
    conductor/ax.py    # AX helpers, selectors, window recovery
    conductor/db.py    # read-only queries, row classification
    conductor/actions.py  # new_workspace, send, queue, set_model, set_effort, archive
    poller.py          # turn tracking → status edits
    store.py           # bridge.sqlite
    config.py
  launchd/com.sameer.slack-conductor.plist
  tests/              # selector snapshot tests against a saved AX tree; row classification fixtures
```

## Milestones

1. **AX + DB adapter CLI**: `sc new <repo> "<prompt>"`, `sc send <ws> "<text>" [--queue|--steer]`, `sc tail <session>`, `sc archive <ws>`. No Slack.
2. **Slack loop**: mentions → adapter → status message → result. Owner gate. Thread map.
3. **Busy follow-ups**: queue detection, pickup, status handoff. `[close]`.
4. **Hardening**: startup reconciliation, AeroSpace recovery, selector self-check, launchd.

## Resolved
- Model/effort: best effort, never blocking (above).
- Repo: always ttd. After creation the bridge still checks `workspace_path` starts with `~/conductor/workspaces/ttd/` and aborts the turn if Conductor fell back to a different repo.
- Session auto-title is not mirrored into Slack.
