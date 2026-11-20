# Connect a client to Flotilla

Ask the Slack app operator for the server’s WebSocket URL and the release revision. The operator enables your Slack member ID in the server’s `pairing_allowed_users` list. Each client gets its own credential. Agent credentials and repository access stay on the machine you pair.

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

## Start and share a session

Mention `@flotilla` in a channel where the app is a member. For example, `@flotilla [profile:daytona] summarize the README` starts a thread on your Daytona client. Omit the selector to use your default. In a DM, you can omit the mention.

The first registered person to summon Flotilla owns the session. Other registered people in the thread can send prompts that run with the owner’s agent credentials and repository access. Only the owner can respond to approvals or questions, interrupt work, close the session, or start a replacement. An offline client keeps its outstanding requests. Existing threads keep their original client when you change your default.

DM `profiles` to list your clients, `default mac` to choose the default for new threads, or `disconnect daytona` to disconnect that credential. Use `[questions]` in the original thread to see outstanding requests. Flotilla sends the details to the owner privately. The headless runtime queues follow-ups and reports interactive controls it cannot perform.
