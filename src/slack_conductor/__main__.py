import argparse
import json
import time
from pathlib import Path

from .conductor.actions import Conductor
from .conductor.db import classify
from .config import Config


def main():
    parser = argparse.ArgumentParser(description="Slack bridge to local Conductor")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
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
    conductor = Conductor(config)
    options = {
        "model": config.model,
        "effort": config.effort,
        "fast": False,
        "send_mode": getattr(args, "send_mode", None) or config.send_mode,
    }
    if args.command == "doctor":
        conductor.ax.check()
        conductor.db.workspace_ids()
        print("Conductor database readable; Accessibility selectors available")
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
        cursor = 0
        while True:
            for row in conductor.db.messages(args.session, cursor):
                for kind, text in classify(row):
                    print(f"{kind}: {text}", flush=True)
                cursor = row["seq"]
            time.sleep(1)


if __name__ == "__main__":
    main()
