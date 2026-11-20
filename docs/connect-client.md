# Connect a client to Flotilla

Ask the Slack app operator for the server’s WebSocket URL and the release revision. The operator can enable your Slack member ID in the server’s `pairing_allowed_users` list or enable pairing for members of this Slack workspace. Each client gets its own credential. Agent credentials and repository access stay on the machine you pair.

To allow current and future active human members of the server’s Slack workspace to pair, set `pairing_allow_workspace_members = true` in the server config. The app uses its `users:read` scope to verify membership when someone DMs a pairing code. External Slack Connect users, deactivated accounts, and bots cannot pair through this setting. The owner and explicitly allowed member IDs keep their existing access.

## Pair a Mac

Open Conductor, add the repository you want the agent to work in, and sign in to the agent you plan to use. You’ll need Git and uv. Clone the [Flotilla repository](https://github.com/zeddybot/flotilla) and check out the revision your operator supplied. Run these commands from the Flotilla checkout. Substitute your server address and the absolute path of the repository you added to Conductor.

```sh
uv sync --frozen
uv run flotilla client connect wss://YOUR-SERVER --profile mac --repo /path/to/your/repo
```

Send the displayed `connect <code>` in a direct message to Flotilla from your own Slack account. The code expires after ten minutes. Return to the same checkout and finish setup.

```sh
uv run flotilla client connect --check
uv run flotilla --root "$HOME/.config/flotilla" client install
uv run flotilla --root "$HOME/.config/flotilla" doctor
```

The client starts at login. Allow its Python interpreter under macOS System Settings → Privacy & Security → Accessibility if the native attachment check requests it. For a custom Conductor workspace location, pass `--workspace-root /path/to/workspaces` during pairing. Flotilla uses that directory to validate workspaces.

## Pair a headless Daytona client

Use a separate sandbox with your repository, the chosen engine CLI and its own working sign-in. Run the following inside the sandbox, from a checkout of the same Flotilla release. Choose `claude` or `codex` and the permission policy suitable for that sandbox.

```sh
uv run flotilla --root /data/config client connect wss://YOUR-SERVER \
  --profile daytona --runtime-adapter headless --engine codex \
  --repo /data/repos/project --workspace-root /data/workspaces --state-dir /data/state
```

DM the displayed code to Flotilla. Run `uv run flotilla --root /data/config client connect --check`. Use `uv run flotilla --root /data/config client run` to start the worker in the foreground, or use the bundled worker supervisor. Run `doctor` in another shell with the same config root. Each paired client has one runtime and one default engine. [Worker deployment](headless-worker.md)

## Configure a client

A profile points to one client and one repository. Choose a profile name when you pair. For a different repository, clone it on the client host and set the absolute `repo` path in the client's `config.toml`. On a Mac, add the checkout to Conductor as well. Restart the client after editing the file. Existing Slack threads keep their sessions and worktrees. Keep those worktrees accessible or close their sessions before moving them.

To use two repositories side by side, run a second client with its own profile name, config root, state directory and workspace root. Use `--root` consistently when pairing and running each instance. The Mac login installer manages one LaunchAgent per user. Run an additional instance with `client run` or give it a separate service.

| Setting | Where to change it |
|---|---|
| Default profile for new threads | DM `default <profile>` to Flotilla. DM `profiles` to see your choices. |
| Headless engine, model and effort defaults | Set `engine` (`claude` or `codex`), `engine_model` and `engine_effort` in the worker config. Blank model and effort use the native CLI defaults. Sign in to each engine you want to use. |
| Follow-up behavior | Slack follow-ups steer when the session supports it. Prefix an individual Slack message with `[queue]` to run it after the active turn. For CLI sends, set `send_mode = "steer"` or `"queue"` in the client config. |
| Mac model and effort | Pick them when starting a Slack session. Untagged Mac sessions use the Slack server's configured model and effort. |
| Headless tool permissions | Set `engine_permission_policy` on the worker. See the [worker permission modes](headless-worker.md#operate-sessions). |
| Repository tools and credentials | Install and authenticate the tools on the client host. Provision any ignored files a fresh Git worktree needs. Flotilla pairs the client with Slack; it does not copy credentials between machines. |

## Start and share a session

Mention `@flotilla` in a channel where the app is a member. For example, `@flotilla [profile:daytona] summarize the README` starts a thread on your Daytona client. Omit the selector to use your default. In a DM, you can omit the mention.

The first registered person to summon Flotilla owns the session. Other registered people in the thread can send prompts that run with the owner’s agent credentials and repository access. Only the owner can respond to approvals or questions, interrupt work, close the session, or start a replacement. An offline client keeps its outstanding requests. Existing threads keep their original client when you change your default.

DM `profiles` to list your clients, `default mac` to choose the default for new threads, or `disconnect daytona` to disconnect that credential. Use `[questions]` in the original thread to see outstanding requests. Flotilla sends the details to the owner privately. Use `[haiku]`, `[sonnet]`, `[opus]`, `[fable]`, `[luna]`, `[terra]`, `[sol]`, or `[astra]` to select a model on a Mac or headless profile. Model availability depends on the signed-in account. On a headless profile you can also specify a native model ID, such as `[model:gpt-6.2-sol]`. Choose the engine, model and effort when starting a session. To change them in an existing thread, start a fresh session with `[new]`.

```text
@flotilla [profile:daytona][sol][high] investigate the failing test
@flotilla check the timestamp parsing before changing the test
@flotilla [queue] run the focused tests after that
@flotilla [new][opus] inspect the fix in a fresh workspace
```

Steering sends your correction to the running native turn. Queueing saves a separate turn to run afterward. Headless clients support both modes. Interactive session attachment and approval replies require Conductor.
