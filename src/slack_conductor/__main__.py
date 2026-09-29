import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from urllib.request import Request, urlopen

from .config import Config, remote_credentials


def main():
    parser = argparse.ArgumentParser(description="Conductor Slack server and Mac client")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("server")
    sub.add_parser("client")
    new = sub.add_parser("new")
    new.add_argument("repo", type=Path)
    new.add_argument("prompt")
    send = sub.add_parser("send")
    send.add_argument("workspace")
    send.add_argument("prompt")
    modes = send.add_mutually_exclusive_group()
    modes.add_argument("--queue", dest="send_mode", action="store_const", const="queue")
    modes.add_argument("--steer", dest="send_mode", action="store_const", const="steer")
    archive = sub.add_parser("archive")
    archive.add_argument("workspace")
    tail = sub.add_parser("tail")
    tail.add_argument("session")
    args = parser.parse_args()
    config = Config.load(args.root)
    if args.command in {"server", "client"}:
        config = replace(config, mode=args.command)
        token = remote_credentials(config)
        if args.command == "server":
            from .remote import RemoteConductor
            from .service import server

            server(args.root, config, RemoteConductor(config, token))
        else:
            from .service import client

            client(args.root, config, token)
        return
    if args.command == "doctor" and config.mode == "server":
        token = remote_credentials(config)
        host = "127.0.0.1" if config.server_host in {"0.0.0.0", "::"} else config.server_host
        if ":" in host:
            host = f"[{host}]"
        request = Request(
            f"http://{host}:{config.server_port}/health",
            headers={"Authorization": "Bearer " + token},
        )
        with urlopen(request, timeout=3) as response:
            ready = json.load(response).get("ready") is True
        print(f"Server health: {'ready' if ready else 'Mac client unavailable'}")
        if not ready:
            raise RuntimeError("Remote Mac client is not ready")
        return
    if config.mode != "client":
        parser.error(f"{args.command} requires the Mac client configuration")
    from .conductor.native import Conductor

    conductor = Conductor(config, client=True)
    options = {
        "model": config.model,
        "effort": config.effort,
        "fast": False,
        "send_mode": getattr(args, "send_mode", None) or config.send_mode,
    }
    if args.command == "doctor":
        conductor.db.query("SELECT id FROM repos LIMIT 1")
        attached = conductor.transport.online()
        print(
            f"Conductor database readable; native frontend {'attached' if attached else 'not ready'}"
        )
        status_file = config.state_dir / "remote-status.json"
        connected = True
        if not status_file.exists():
            connected = False
            print("Remote link: no status")
        elif status_file.exists():
            status = json.loads(status_file.read_text())
            fresh = time.time() - status.get("at", 0) <= 60
            connected = fresh and status.get("connected", False)
            link = "connected" if connected else "stale" if not fresh else "disconnected"
            print(f"Remote link: {link}; client: {status.get('client_id', 'unknown')}")
        if not attached:
            raise RuntimeError("Native attachment is not ready; check daemon.log")
        if not connected:
            raise RuntimeError("Remote server link is not connected; check daemon.log")
    elif args.command == "new":
        if args.repo.resolve() != config.repo.resolve():
            parser.error("repo must match config.toml")
        workspace = conductor.new_workspace()
        print(
            json.dumps(
                {"workspace_id": workspace["id"], "session_id": workspace["active_session_id"]}
            ),
            flush=True,
        )
        print(conductor.send(workspace["id"], workspace["active_session_id"], args.prompt, options))
    elif args.command == "send":
        workspace = conductor.validate(args.workspace)
        print(conductor.send(workspace["id"], workspace["active_session_id"], args.prompt, options))
    elif args.command == "archive":
        workspace = conductor.validate(args.workspace)
        print("archived", conductor.archive(workspace["id"], workspace["active_session_id"]))
    elif args.command == "tail":
        from .conductor.db import classify

        cursor = 0
        while True:
            for row in conductor.db.messages(args.session, cursor):
                for kind, text in classify(row):
                    print(f"{kind}: {text}", flush=True)
                cursor = row["seq"]
            time.sleep(1)


if __name__ == "__main__":
    main()
