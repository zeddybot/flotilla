# Future clients, engines, and hosts

This record preserves the original headless-worker design. The Claude Code and
Codex worker, pairing, and profile routing have since shipped. For current setup,
see [Worker deployment](headless-worker.md) and [Pair a client](connect-client.md).

The first proposed additional runtime is a dedicated Daytona worker containing
Claude Code and Codex CLIs. This is separate from the always-on Slack server.
The worker connects outbound using the session-runtime contract and owns its
repository, agent credentials, process lifecycle, receipts, and event journal.


The September follow-up explicitly permits a headless pilot. That is a scoped
exception to the earlier design's requirement that every session be visible in
an interactive native app/TUI. A copied transcript is not an interactive attach.
Advertise `reveal=false` until resuming the exact same session in a real terminal
has been qualified; do not launch a replacement conversation to simulate it.

## First vertical slice

1. Pin CLI versions in a worker image without embedding credentials. Keep the
   Flotilla control server and execution worker independently replaceable.
2. Give a workspace provider ownership of repository/worktree creation, roots,
   and archival policy. Give a process host ownership of cwd, environment,
   process groups, stdout/stderr, exit status, and cancellation. The engine
   adapter owns prompts, native conversation IDs, configuration, and event
   semantics. Conductor remains an integrated runtime rather than being forced
   through these terminal abstractions.
3. Start with explicit engine selection at session creation and one active
   process per session. Queue additional messages durably. Initially reject
   steering unless the selected engine supports actual mid-run input; killing
   and restarting a process does not implement steering.
4. Persist native conversation IDs before acknowledging creation, plus command,
   message, run, event, and job correlation. Resume by the explicit stored ID,
   never a directory-global "last session" selector. Recover an ambiguous launch
   by inspecting native state instead of blindly rerunning a prompt.
5. Translate incremental output, tool activity, approvals, results, background
   jobs, and errors into the runtime contract. A successful process exit is only
   one observation: prove that no owned background work or callback remains.
6. Surface capability and configuration gaps before accepting a request. Define
   an explicit noninteractive approval policy; never silently enable blanket
   permission bypass. Unsupported interactive questions must become actionable
   attention states, not an apparent hang or fabricated answer.

## CLI examples from the original proposal

Claude Code's print mode can emit structured streaming output:

```sh
claude -p 'prompt' --output-format stream-json --verbose --include-partial-messages
claude -p 'follow-up' --resume SESSION_ID --output-format stream-json --verbose
```

Codex uses `exec` for noninteractive work; its `-p` flag selects a configuration
profile, not print mode:

```sh
codex exec --json 'prompt'
codex exec resume SESSION_ID --json 'follow-up'
```

The current worker uses bidirectional Claude stream-json and the Codex app-server.
The examples above show the one-shot commands considered during planning. Primary references are [Claude Code programmatic usage](https://code.claude.com/docs/en/headless)
and [Codex noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
Claude and Codex JSON events differ; parsing JSON alone does not establish
equivalent session, interruption, approval, or background-job semantics.

## Original qualification plan and follow-up ideas

Use a disposable repository and validate create, explicit-ID resume, repeated
prompt identities, durable queueing, text/tool events, actual model settings,
files, permissions, close/archive, process crash, worker restart, server outage,
and receipt/event replay. Include ambiguous-send recovery and cancellation while
an external command is still running. Declare steering, approvals, and interactive
attach unsupported until those specific behaviors pass end-to-end checks.

After one headless engine passes, add the second engine under the same process
host. Next, qualify a tmux host and interactive attachment to those same engine
sessions. A later cmux host can reuse engine semantics while owning pane/tab
identity and reveal. Desktop clients such as Codex Desktop, Claude Desktop, or
T3 Code each need independent control/recovery research. Support is a tested
engine/host/version combination, not every combination of installed components.

Paired clients now have separate credentials and profile routing. The first
Slack user to start a thread owns the session. Registered collaborators can
submit prompts, while sensitive session controls remain owner-only. See
[Start and share a session](connect-client.md#start-and-share-a-session).
