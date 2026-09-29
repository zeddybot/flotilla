# Flotilla: design decisions and discussion record

Recorded September 9, 2026. This captures the product and architecture discussions that followed the personal Slack–Conductor bridge. It is a design document, not a claim that the multi-user system exists. The Slack app remains Conductor and owner-only. The September follow-up adds the `flotilla` CLI while preserving `sc`; see [the implemented CLI](flotilla-cli.md) and [runtime boundary](runtime-interface.md) for current branch behavior, which is separate from deployment status.

September 29 scope refinement: generalize the existing runtime, polish single-owner installation, and add local incident logs/CLI monitoring. Keep GitHub private and use local CI. Public-release preparation and additional clients/engines/hosts are plans only. The first future Daytona worker may deliberately be headless; [that plan](future-runtimes.md) records its visibility/capability limitations. The multi-user CLI proposal below remains a future design, not the current command reference.

Operational follow-up: [Socket Mode reconnect incident and recovery](connection-incident.md).

## Decision status

**Agreed:** Flotilla is the private working name; one shared Slack app; a central server; user identities with multiple clients; one or more profiles per client; routing by the initiating user's profile; per-user default profile; collaborative thread follow-ups to the original session; sessions visible in the user's actual app/TUI; extensible client plugins; model, effort, fast, and verbose resolved at session creation.

**Proposed implementation details:** the precise CLI spelling, bracket syntax for profiles, authentication flow, plugin wire protocol, database choice, hosting configuration, additional administrative commands, and permission policy for collaborators. These are recommendations from the discussion, not separately approved implementation contracts.

**Preserve:** the current bridge's queue/steer behavior, message placement, lifecycle tracking, restart recovery, mention gating, long-answer formatting, and native-session continuity. The intentional product changes are multi-user collaboration and session-start configuration; do not accidentally retain owner-only gating in the shared server or require live setting changes from every new plugin.

## Name and scope

The project is no longer specific to Conductor. Bay was the initial candidate: a nautical home base/port dispatching agents as ships. Flotilla better expresses different people bringing different vessels and agents to a shared system. Harbor was rejected because the user associates it with an existing agent-evaluation product.

A live product-name search found Flotilla in use by an [AI orchestration project](https://bigbearengineering.com/), [community software](https://flotilla.social/articles/what-is-flotilla), and [fleet-management software](https://flotillaiot.com/). Bay is used by an [agent-oriented deployment platform](https://thebay.cloud/), [endpoint security platform](https://bay.security/), and the nearby [BayChat](https://baychat.io/) name. The user accepted Flotilla anyway because this project will remain private for the foreseeable future. No trademark or domain clearance was performed.

Other brainstormed names included Portside, Berth, Tiller, Cove, Jetty, Mooring, Launch, Beacon, Sound, Skiff, Relay, Rally, Switchboard, Anchorage, Quay, Marina, Wharf, Dockside, Haven, Roadstead, Breakwater, Helm, Heading, Bearing, Waypoint, Passage, Fairway, Channel, Compass, Fleet, Armada, Convoy, Crew, Bosun, Lookout, Pilot, Tender, Tide, Current, Shoal, Seabound, Bluewater, Drift, Wake, Buoy, Dispatch, Junction, Rendezvous, Commons, Huddle, Tandem, Patchbay, Loom, Threadline, Concourse, Outpost, and Crossbar. These were ideas, not availability-checked alternatives.

This remains a side project. The original discussion avoided work tickets; the current workspace instructions now require a ticket for PRs intended for main. A future public launch or product rename is a separate action from adopting this working name.

## Users, clients, profiles, and routing

```text
Server / Slack workspace
└── Sameer
    ├── macbook client
    │   ├── conductor profile ← user's default
    │   └── cmux profile
    └── daytona client
        ├── cloud profile
        └── experiments profile
```

A user can register several clients. Each profile belongs to exactly one client. The server resolves the initiating Slack user's profile and dispatches to its owning client. Profile names should be unique within a user's account; different users may each have `cloud`. Identity must include the Slack workspace as well as user ID.

The initiating user is the first authorized person to mention Flotilla in an unmapped thread, even if someone else wrote the thread's parent bug report. A top-level message is not required. Subsequent authorized collaborators mentioning Flotilla in that thread reach the same owner/client/profile/session. Their own defaults do not affect routing. Ordinary conversation between coworkers in the channel thread is ignored. DMs remain mention-free.

Proposed examples:

| Request | Resolution |
| --- | --- |
| `@flotilla investigate this` | Initiator's default profile and that profile's defaults |
| `@flotilla [profile:cloud] investigate this` | Initiator's cloud profile and owning client |
| `@flotilla [profile:cmux][astra] investigate this` | Initiator's cmux profile and its Astra route |
| `@flotilla [astra] investigate this` | Initiator's default profile and its Astra route |

The exact `[profile:...]` syntax is proposed. Bindings use stable IDs, not mutable names. Changing defaults or renaming a profile affects future resolution, not an existing conversation. A profile being offline produces an explicit waiting/offline state; never silently run the task on another user's machine or select a different profile.

The server records both the session owner and each message's author. Collaboration runs against the owner's agent credentials and resources. Which teammates may send prompts, answer approvals, interrupt, close, or create replacement sessions needs an explicit policy. Prompt collaboration does not automatically grant configuration, credential, or device-management rights.

## Session configuration: latest agreed refinement

Resolve configuration once: **profile defaults → opening-message overrides → fixed session specification**. Model, effort, fast, and verbose do not need to be reconfigured mid-session. This supersedes the earlier proposal for applying settings separately to each queued request or requiring a generic live `configure` method.

Example: `@flotilla [profile:work][astra][high][fast][verbose] investigate this` selects the client, runtime, engine, model, effort, fast mode, and verbosity at creation. Every follow-up inherits that resolved specification. The runtime validates it and reports effective values.

| Option/action | Lifetime |
| --- | --- |
| Profile, model, effort, fast, verbose | Session creation |
| Queue / steer | Follow-up delivery; preserve the current sticky send-mode behavior |
| New | Create a fresh session, optionally with new settings |
| Close | Close/archive the existing bound session |

Proposed handling of changed configuration tokens on a follow-up: explain that `[new]` is required; do not silently ignore the change or replace the engine. Repeating the current setting can be a no-op. Changes made directly in the app should be observed and reported where possible; Flotilla should not fight local controls. Exact reconciliation of such local changes remains to be specified.

A profile may route Astra to Codex and Fable to Claude Code, both inside cmux. Another profile might route the same model aliases through OpenCode or Pi. Model aliases therefore are not global engine assignments. Resolve the engine at session start and retain it. A cross-engine handoff is a separate feature and must not be simulated as steering.

Supported effort levels vary by engine/model/version; advertise and explicitly map them. Preserve current bracket names where supported. Fast is a distinct execution option, not lower effort or a cheaper model. Verbose controls presentation of available tool/activity events, not disclosure of private reasoning.

The current Conductor adapter continues on model/effort/fast selection mismatch with a visible note of actual values. Preserve that as an explicit compatibility policy; do not claim settings were applied when they were not. The future contract must distinguish unsupported configurations discovered before launch from unexpected selection failures.

## Machines and native user interfaces

All current teammates use MacBooks. A client may run on a Mac or persistent remote machine, including a physical box or Daytona sandbox. Linux scope is initially terminal agents in tmux, without GUI automation.

The desired integrations include Conductor with AeroSpace; Claude Code and Codex in cmux; Claude Desktop; Codex Desktop; Claude Code, Codex, OpenCode, or Pi TUIs; iTerm, Ghostty, and tmux hosts; T3 Code; and OpenCode inside Conductor. Supporting an agent and supporting it inside a particular application are separate compatibility claims.

Sessions must appear in the user's actual app/TUI and remain usable there. A hidden headless agent with a copied log view does not meet that requirement. Stable session/pane/workspace identity is necessary for follow-ups, recovery, and `session open`.

Prefer supported control APIs and native events where they exist. Keyboard or Accessibility control remains a valid implementation technique where required, paired with explicit target/draft validation and reliable observation. A general text-in/text-out terminal interface cannot establish true steering, task completion, or approval semantics by itself.

Integrated applications may own workspace, model selection, session control, and observation together. Conductor should be implemented as an integrated runtime rather than forced through a terminal abstraction. Each other desktop application requires investigation; this document does not assert that its current interfaces support full parity.

## Server, storage, and transport

One central service owns Slack credentials and the Slack connection, identities, profile routing, thread bindings, authorization, and Slack presentation. Clients connect outbound over an authenticated persistent connection, which avoids requiring inbound access to teammates' laptops. Each client retains local agent credentials and configuration. Plugins do not receive the organization's Slack credentials merely to control an agent.

The initial recommendation is a single service with local SQLite, durable state, and backups. This is still a proper application architecture: authentication, acknowledgment, recovery, and schema migrations matter regardless of database size. A single local Postgres is possible but adds operations; managed Postgres is appropriate when replication, multiple server instances, or other operational needs justify it. No database choice is finalized.

SQLite's active database should live on a suitable local filesystem. Do not assume a network/FUSE/object-storage-backed Daytona volume provides the locking semantics needed for SQLite WAL. External storage is useful for backups, with tested restore behavior. Container filesystem persistence does not preserve running processes after stop/start.

Daytona is an acceptable pilot host based on the [live transport qualification](daytona-transport.md). A small persistent VM is another option. Disable auto-stop for an always-on server and configure process supervision. VM auto-pause is a separate lifecycle setting if VM sandboxes are used. Ephemeral/TTL/auto-delete policies must match the intended retention. Credits make Daytona attractive for the pilot but do not establish availability guarantees.

Daytona private preview credentials and application client authentication are different layers. Standard preview tokens rotate on sandbox restart; signed URLs have bounded expiry. Do not distribute organization API keys to clients. Production URL/authentication design remains open.

Slack Socket Mode and the client-to-Flotilla transport are separate connections. Slack periodically reconnects Socket Mode; its documentation recommends evaluating HTTP event delivery for shared production apps. Socket Mode remains usable for the pilot. Neither approach removes the need for durable application delivery.

## CLI proposal

One `flotilla` executable can operate a server or a client. This section is the broader future proposal; [the CLI guide](flotilla-cli.md) lists the implemented single-owner subset and exact command spelling.

`flotilla init` should guide the user through a server URL, browser authentication/pairing to prove their Slack identity, device naming, installed-tool detection, profile creation, default selection, and optional automatic startup. Each wizard step should also have a scriptable command.

| Area | Proposed commands |
| --- | --- |
| Server | `flotilla server init/start/stop/restart/status/logs` |
| Client | `flotilla client connect <url>`, `disconnect/start/stop/restart/status/logs` |
| Automatic client startup | `flotilla client install/uninstall` |
| Profiles | `flotilla profile add/list/show/edit/remove/default/test` |
| Diagnostics | `flotilla status`, `flotilla doctor`, `flotilla config validate` |
| Sessions | `flotilla session list/show/open/stop` |
| Devices | `flotilla devices list/revoke` |

Use structured `--json` output for automation. `doctor` checks server connectivity, auth, binaries, compatible versions, and permissions needed by the selected runtime. `profile test` should exercise a disposable real session through send, output, and completion. Device credentials are independently revocable; adding another client joins the same user account and adds profiles.

Proposed command semantics: `server stop` operates on the server on that host. `client stop` stops the bridge while preserving agent sessions. Stopping a session is distinct from archiving a workspace or deleting a machine. `session open` reveals the existing native session. The exact cleanup policy belongs to the selected runtime/profile and should be visible.

macOS automatic startup for UI adapters means a user LaunchAgent at login, not a system daemon capable of controlling a desktop before login. launchd is analogous to systemd in its service-supervision role; they are different operating-system facilities. Linux clients need an appropriate service setup and tmux session lifetime. Current installation details are in the [README](../README.md).

## Plugin boundaries

The public client-plugin boundary is a **controllable, observable session runtime**. Reusable interfaces beneath it separate workspace creation, app/terminal hosting, agent semantics, and observation. A package may implement several interfaces; this is not a requirement to install many tiny plugins.

| Component | Responsibility |
| --- | --- |
| Server | Slack policy/presentation, identity, profile resolution, thread binding |
| Client core | Durable commands/events, local profile resolution, ownership, recovery, locks |
| Session runtime | Create/recover/control/observe a real native session |
| Workspace provider | Repository/directory selection, worktrees, supported archive/cleanup |
| Host | Windows, tabs, panes, app workspaces, native-session reveal |
| Agent integration | Model/effort/fast semantics, sending, steering, interruption, output |
| Observation | Translate APIs, hooks, logs, or read-only native state into normalized events |

Thus Claude Code can reuse an agent integration under cmux or tmux, while a Conductor runtime can own its whole integration. Only tested combinations count as supported; do not advertise an untested Cartesian product of hosts and engines. macOS Accessibility and desktop recovery are platform utilities; Linux tmux profiles need no GUI layer.

Proposed runtime operations are `probe`, `create`, `recover`, `submit`, `observe`, `respond`, `interrupt`, `close`, and `reveal`. `probe` reports capabilities and prerequisites; `create` takes the resolved session specification; `recover` reattaches to a durable native binding. `respond` answers a specific question/approval where supported. Live `configure` is no longer a baseline requirement after the session-start decision.

Prefer versioned structured commands/events over a local process boundary, permitting plugins in different languages and independent restarts. This is a proposed implementation, not a selected RPC library. Process separation supplies fault isolation, not a security sandbox. Plugins are trusted local programs; installation and arbitrary executable configuration must not become abilities implicitly granted by a Slack prompt.

## Identity, events, and state

Keep workspace, session, run, message, and background-job identities distinct. A session is the persistent conversation; a run is one logical piece of work that can include several native turns and callbacks. A steer adds a message to the active run; a queued follow-up starts another run when eligible. Store native identifiers as plugin-owned bindings. Do not equate one native turn ID with one logical run.

Commands must distinguish received, queued, and picked up. A successful keypress or API acceptance does not prove the agent started the request. Journal command IDs and native receipts; give each command one execution owner. Track a native queue rather than also dispatching an independent copy. Ambiguous sends are reconciled, never blindly replayed.

Events carry session/run/message/job correlation, source identity, ordering/cursor information, and replay-safe identity. Normalize text updates, tools, activity, questions/approvals, job lifecycle, result candidates, native pickup, actual configuration, and confirmed rest. Plugin snapshots repair state after reconnect; stale or incomplete observations must be explicit.

Track connectivity, session lifecycle, message delivery, run activity, outcome, background work, and observation health separately. They coexist: a run may await a background job while its client disconnects and another message is queued. Server presentation chooses a single user-facing header without discarding those facts.

Serialize session control actions; also serialize conflicting desktop automation across plugins that share focus/UI resources. Observation and unrelated terminal sessions should continue concurrently. Prefer native event streams; where the only reliable source is local polling, keep it incremental and bounded. Slack edits remain coalesced and respect rate limits.

## Existing behavior: compatibility checklist

The [README](../README.md) and implementation are the current operational reference. [SPEC.md](../SPEC.md) also contains historical implementation notes and is not a future multi-user spec.

- Channel commands require explicit mention, including follow-ups; DMs do not. A mention in an existing bug-report thread can start a session. Current owner/workspace filtering and bot/subtype filtering remain in the personal adapter until deliberately replaced by shared-user authorization.
- Current tokens are model aliases (`opus`, `sonnet`, `haiku`, `fable`, `astra`, `sol`, `terra`, `luna`), effort (`low`, `medium`, `high`, `xhigh`, `max`, `ultracode`), `fast`, `verbose`, `queue`, `steer`, `new`, and `close`. Aliases are configuration-driven. Tokens lead the prompt and are case-insensitive; unknown bracket text remains prompt text. Current parser stops at the first unknown bracket token.
- Steering is the default. Queue and steer are sticky per thread. An idle agent starts either immediately; a busy steer joins the current run, while queue waits for separate work. Never implement steer as an undisclosed abort-and-resend.
- Current model/effort/fast/verbose settings are sticky and reapplied per message. The future session-start configuration intentionally supersedes this; do not describe it as deployed before implementation.
- Each primary Slack reply has emoji/status, optional detail, and optional content. Final-answer overflow replies contain only content, without repeating the status header. Preserve Creating workspace, Starting, Thinking, Working, Waiting, Queued, Done, Needs attention, Failed, Reconnecting, Archiving, and Archived. Status changes preserve the latest agent text. Questions and actionable bridge problems produce Needs attention; do not infer blocked states from prose.
- One evolving message is preferred. A background wait shows both its waiting/job status and the previous agent content. Verbose tool messages and long-answer continuations use the same header format. Preserve code fences and bounded message sizes; coalesce status edits and honor Slack retry delays.
- A result with tracked jobs is interim. Job success/failure/cancellation may resume the agent; keep observing through the callback and next result, including callbacks under a newer native turn ID. Done requires a final candidate, no outstanding owned jobs, no pending callback, and confirmed rest. The current adapter uses a two-second settle period. No arbitrary job timeout may force Done.
- Confirmed steering pickup transfers content and job tracking into the new acknowledgment below the user's steer. Populate the replacement before deleting the old reply; persist cleanup across crashes and repeated steers. Queueing keeps the previous reply through Done and creates a separate reply for the queued run.
- `[new]` remaps the thread to a new workspace while older active work retains its result destination. `[close]` archives the mapped Conductor workspace and can stop its running agent. Later follow-ups require `[new]`. Other runtimes must expose a clear equivalent lifecycle rather than silently deleting unrelated resources.
- Prompts carry Slack channel, thread timestamp, message timestamp, and permalink. The current bridge tells the agent to fetch additional context with its Slack tools and not post replies itself. It does not copy all channel history. The shared system additionally needs message-author attribution and a deliberate context-access policy for clients without Slack tools.
- Unrelated user-started native turns and foreign/nested agent output are not relayed as this Slack run's answer. A subsequent turn may occupy a session while the previous run's result is being delivered.
- Durable deduplication uses Slack workspace/channel/message identity. Restart resumes queued/running work, partial output, job tracking, and steering cleanup. UI actions and SQLite cannot commit atomically; do not claim exactly-once execution.
- Current Conductor actions use Accessibility with read-only database observation. Validate workspace/session identity, repository roots, and composer drafts before sending. Do not overwrite an unsent draft or write Conductor's database. UI recovery may briefly switch desktops through AeroSpace.

## Delivery approach and open decisions

First wrap the current Conductor runtime and extract shared behavior with regression parity. Then prove one composed terminal integration and reuse its agent integration under another host. Qualify each engine/host/version against the same lifecycle, queue/steer, configuration, visibility, recovery, and close contract. Capability declarations expose gaps; they do not license silent degradation.

Open decisions include exact client authentication/pairing, collaborator control permissions, repository selection, profile syntax, defaults inherited by `[new]`, native-setting drift, offline-command expiry/cancellation, private server URL/token renewal, storage/backups, plugin packaging/protocol, and which additional integrations can meet full parity. Missing native controls may require hooks or UI integration; do not promise capabilities solely because a tool has an API or a headless mode.

Useful primary references: [cmux API](https://cmux.com/docs/api), [OpenCode server/TUI/events](https://opencode.ai/docs/server/), [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md), [Claude Code channels](https://code.claude.com/docs/en/channels), [T3 Code internals](https://github.com/pingdotgg/t3code/blob/main/docs/internals/overview.md), and [Slack HTTP vs Socket Mode](https://docs.slack.dev/apis/events-api/comparing-http-socket-mode/). These interfaces evolve; inspect current versions when building an integration.
