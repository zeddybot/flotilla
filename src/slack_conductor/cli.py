"""The host-local Flotilla command line."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import cli_output
from .cli_config import CLIError, discover_root, init_config, load_config, validate_config


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message, "usage_error")


def _parser():
    parser = Parser(prog="flotilla", description="Operate the Flotilla server and Mac client")
    parser.add_argument("--root", type=Path, help="directory containing config.toml")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    init = commands.add_parser("init", help="create a single-owner Mac client config")
    init.add_argument(
        "--server-url", required=True, help="existing server URL (wss, or ws for localhost)"
    )
    init.add_argument("--token-file", type=Path, required=True, help="existing private token file")
    init.add_argument("--repo", type=Path, required=True, help="repository to use in Conductor")
    init.add_argument("--workspace-root", type=Path, required=True)
    commands.add_parser("status", help="show local service and connection status")
    commands.add_parser("doctor", help="check configured service health")
    monitor = commands.add_parser("monitor", help="show local monitoring report")
    monitor.add_argument("--watch", action="store_true")
    monitor.add_argument("--interval", type=float, default=10)
    config = commands.add_parser("config")
    config.add_subparsers(dest="action", required=True, parser_class=Parser).add_parser("validate")
    for host in ("server", "client"):
        group = commands.add_parser(host)
        actions = group.add_subparsers(dest="action", required=True, parser_class=Parser)
        allowed = ("run", "start", "stop", "restart", "status", "logs")
        if host == "client":
            allowed += ("install", "uninstall")
        for action in allowed:
            child = actions.add_parser(action)
            if action == "logs":
                child.add_argument("--follow", action="store_true")
                child.add_argument("--lines", type=int, default=50)
            elif host == "client" and action == "install":
                child.add_argument("--prepare-only", action="store_true")
    session = commands.add_parser("session")
    actions = session.add_subparsers(dest="action", required=True, parser_class=Parser)
    actions.add_parser("list")
    for action in ("show", "open", "stop", "archive"):
        child = actions.add_parser(action)
        child.add_argument("session_id")
        if action != "show":
            child.add_argument("--command-id")
    new = actions.add_parser("new")
    new.add_argument("prompt")
    new.add_argument("--repo", type=Path)
    new.add_argument("--command-id")
    new.add_argument("--engine", choices=("claude", "codex"), help="engine on a headless worker")
    new.add_argument("--model", help="model ID for this session")
    _send_mode(new)
    send = actions.add_parser("send")
    send.add_argument("session_id")
    send.add_argument("prompt")
    send.add_argument("--command-id")
    _send_mode(send)
    tail = actions.add_parser("tail")
    tail.add_argument("session_id")
    tail.add_argument("--after", type=int, default=0)
    tail.add_argument("--follow", action="store_true")
    respond = actions.add_parser("respond", help="answer a pending runtime question or approval")
    respond.add_argument("session_id")
    respond.add_argument("question_id")
    respond.add_argument("--command-id")
    reply = respond.add_mutually_exclusive_group(required=True)
    reply.add_argument(
        "--answer",
        action="append",
        help="answer each question in order; repeat for multiple questions",
    )
    reply.add_argument("--approve", action="store_true")
    reply.add_argument("--deny", action="store_true")
    reply.add_argument(
        "--response-file", type=Path, help="JSON object for structured native responses"
    )
    return parser


def _send_mode(parser):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--queue", dest="send_mode", action="store_const", const="queue")
    modes.add_argument("--steer", dest="send_mode", action="store_const", const="steer")


def _global_options(argv):
    """Accept global switches before or after a subcommand."""
    leading = []
    rest = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--":
            rest.extend(argv[index:])
            break
        if value == "--json":
            leading.append(value)
        elif value == "--root":
            if index + 1 >= len(argv):
                raise CLIError("--root requires a directory", "usage_error")
            leading.extend((value, argv[index + 1]))
            index += 1
        elif value.startswith("--root="):
            leading.append(value)
        else:
            rest.append(value)
        index += 1
    return leading + rest


def _render(value, json_output, command=None, action=None):
    if json_output:
        print(json.dumps({"ok": True, "data": value}, default=str, sort_keys=True), flush=True)
    elif value is not None:
        print(cli_output.render(value, command, action), flush=True)


def _report(config, root):
    from .monitoring import report

    return report(config, root)


def _dispatch(args, root, config):
    if args.command == "init":
        return init_config(root, args.server_url, args.token_file, args.repo, args.workspace_root)
    if args.command == "config":
        return validate_config(root)
    if args.command in {"status", "doctor", "monitor"}:
        result = _report(config, root)
        if args.command == "monitor":
            from .monitoring import Monitor

            return Monitor(config).check(result)
        if args.command == "doctor" and not result.get("ok", False):
            raise CLIError("Doctor found unhealthy components: " + json.dumps(result), "unhealthy")
        return result
    if args.command in {"server", "client"}:
        from . import service_manager as service

        if args.action == "logs":
            if args.lines < 0:
                raise CLIError("--lines must be nonnegative", "usage_error")
            method = getattr(service, args.command + "_logs")
            return method(root, follow=args.follow, lines=args.lines)
        method = getattr(service, args.command + "_" + args.action)
        if (
            args.command == "server"
            and args.action in {"run", "start", "stop", "restart", "status"}
        ) or (args.command == "client" and args.action in {"install", "run"}):
            result = (
                method(root, prepare_only=args.prepare_only)
                if args.command == "client" and args.action == "install"
                else method(root)
            )
        else:
            result = method()
        if args.action == "run":
            if result:
                raise CLIError(f"{args.command} exited with status {result}", "service_failed")
            return None
        return result
    from . import cli_sessions as sessions

    if args.action == "list":
        return sessions.list_sessions(config)
    if args.action == "show":
        return sessions.show_session(config, args.session_id)
    if args.action == "new":
        return sessions.new_session(
            config,
            args.prompt,
            args.repo,
            args.send_mode,
            args.command_id,
            engine=args.engine,
            model=args.model,
        )
    if args.action == "send":
        return sessions.send(config, args.session_id, args.prompt, args.send_mode, args.command_id)
    if args.action == "tail":
        return sessions.tail(config, args.session_id, after=args.after, follow=args.follow)
    if args.action == "respond":
        if args.response_file:
            if args.response_file.stat().st_size > 64 * 1024:
                raise CLIError("Response file is too large", "usage_error")
            response = json.loads(args.response_file.read_text())
            if not isinstance(response, dict):
                raise CLIError("Response file must contain a JSON object", "usage_error")
        elif args.answer:
            response = {"kind": "question", "answers": args.answer}
        else:
            response = {"kind": "approval", "decision": "allow" if args.approve else "deny"}
        return sessions.respond(
            config, args.session_id, args.question_id, response, args.command_id
        )
    return {"open": sessions.open_session, "stop": sessions.stop, "archive": sessions.archive}[
        args.action
    ](config, args.session_id, args.command_id)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    try:
        args = _parser().parse_args(_global_options(argv))
        json_output = args.json
        streaming = (
            (args.command == "session" and args.action == "tail" and args.follow)
            or (args.command in {"client", "server"} and args.action == "logs" and args.follow)
            or (args.command == "monitor" and args.watch)
        )
        if args.json and args.command in {"client", "server"} and args.action == "run":
            raise CLIError("--json is unavailable for a foreground service", "usage_error")
        if (
            args.json
            and args.command in {"client", "server"}
            and args.action == "logs"
            and args.follow
        ):
            raise CLIError("--json is unavailable while following raw logs", "usage_error")
        if args.command == "monitor" and args.interval < 5:
            raise CLIError("--interval must be at least 5 seconds", "usage_error")
        root = (
            (
                args.root.expanduser().resolve()
                if args.root is not None
                else Path.home() / ".config/flotilla"
            )
            if args.command == "init"
            else discover_root(args.root)
        )
        config = None if args.command == "init" else load_config(root)
        if args.command == "session" and args.action == "tail":
            events = _dispatch(args, root, config)
            if args.follow:
                for event in events:
                    _render(event, args.json, "session", "tail")
            else:
                _render({"events": list(events)}, args.json, "session", "tail")
        elif args.command == "monitor" and args.watch:
            from .monitoring import Monitor

            monitor = Monitor(config)
            while True:
                _render(monitor.check(_report(config, root)), args.json, "monitor")
                time.sleep(args.interval)
        else:
            value = _dispatch(args, root, config)
            if not streaming:
                _render(value, args.json, args.command, getattr(args, "action", None))
        return 0
    except KeyboardInterrupt:
        return 130
    except subprocess.SubprocessError as exc:
        message = (
            f"Service command failed with exit status {exc.returncode}"
            if isinstance(exc, subprocess.CalledProcessError)
            else "Service command failed"
        )
        if json_output:
            print(
                json.dumps({"ok": False, "error": {"code": "service_failed", "message": message}})
            )
        else:
            print(cli_output.error(message, "service_failed"), file=sys.stderr)
        return 1
    except (CLIError, OSError, RuntimeError, ValueError) as exc:
        code = exc.code if isinstance(exc, CLIError) else "command_error"
        if json_output:
            print(json.dumps({"ok": False, "error": {"code": code, "message": str(exc)}}))
        else:
            print(cli_output.error(str(exc), code), file=sys.stderr)
        return 2 if code == "usage_error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
