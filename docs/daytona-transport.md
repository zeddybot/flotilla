# Daytona transport qualification

Verified September 9, 2026, using a disposable ProDex Labs Daytona US container and a local Mac client. The endurance test ran from 06:07:37 to 06:37:37 UTC through Daytona's hosted preview proxy.

| Check | Result |
| --- | --- |
| Idle WebSocket, automatic client/server pings disabled | 1,800 seconds connected; final echo succeeded in 21.5 ms |
| Application echo heartbeat every 20 seconds | 1,800 seconds connected; all 90 replies in order; zero reconnects |
| Heartbeat round-trip latency | Median 21.7 ms; minimum 18.5 ms; maximum 102.1 ms |
| Deliberate echo-server process termination/restart | Existing socket closed; four connection attempts failed during the deliberate outage; a new server generation echoed successfully after 8.443 seconds |
| WebSocket upgrade without application token | HTTP 401 |

Configuration: `daytonaio/sandbox:0.8.0`, container class, 1 vCPU, 1 GiB memory, 3 GiB disk, auto-stop disabled, Python `websockets` 15.0.1 on both sides. Public preview routing carried an echo service protected by a temporary application bearer token. No real Slack messages or credentials were placed in the sandbox, and no organization API key was created.

The initial private setup sandbox and the final echo sandbox were both deleted. A refreshed dashboard filtered to the test names returned no matching sandboxes. Local temporary credentials were removed. Sanitized scripts/results remain in the original workspace's gitignored `.context/daytona-ws/`; this document preserves the portable result without depending on those local artifacts.

## What this establishes

The hosted preview proxy supports the intended long-lived client connection for a pilot. Thirty minutes is the observed duration, not a maximum or an unlimited-lifetime guarantee. The recovery measurement includes an intentional eight-second delay before restarting the echo process; it is not a platform restart-time benchmark.

This did not test full sandbox/infrastructure restart, private-preview credential renewal, Slack Socket Mode, database durability, multi-day uptime, or load. Use heartbeats, reconnection, durable acknowledgments/replay, and process supervision. Disable auto-stop for an always-on container; VM auto-pause is separate.

Daytona documents WebSocket upgrades and publishes a persistent-connection example with health checks and reconnection. Its example's 300-second client ping timeout is not a server-imposed maximum connection lifetime. No published hard maximum lifetime was found in the reviewed sources.

Standard private-preview tokens rotate when a sandbox restarts. Signed URLs embed a token and have bounded expiry (documented default 60 seconds, maximum 24 hours). These mechanisms are separate from Flotilla's client/device authentication. Do not distribute Daytona organization API keys to clients.

## Sources

- [Preview proxy and WebSocket support](https://www.daytona.io/docs/en/custom-preview-proxy/)
- [Preview authentication and token lifetime](https://www.daytona.io/docs/en/preview/)
- [Persistence and lifecycle](https://www.daytona.io/docs/en/persistence/)
- [Persistent WebSocket training example](https://www.daytona.io/docs/en/guides/reinforcement-learning/openenv-finqa/)

The project core moved out of the public Daytona repository in 2026. Historical public proxy constants were not treated as evidence of current production connection limits; current documentation and the live test support the conclusions above.
