"""Private host-local RPC to the service that owns headless agent processes."""

import hashlib
import json
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path

from .runtime import dispatch

MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


def socket_path(state_dir):
    # Unix socket paths are limited to about 100 bytes, including macOS temp roots.
    directory = Path("/tmp") / f"flotilla-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Unsafe local runtime socket directory")
    name = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:24]
    return directory / (name + ".sock")


class RuntimeServer:
    def __init__(self, state_dir, runtime):
        self.path = socket_path(state_dir)
        if self.path.exists():
            with socket.socket(socket.AF_UNIX) as existing:
                existing.settimeout(1)
                if existing.connect_ex(str(self.path)) == 0:
                    raise RuntimeError("A local runtime service is already running")
        self.path.unlink(missing_ok=True)
        serial = threading.Lock()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(40)
                try:
                    raw = self.rfile.readline(MAX_REQUEST + 1)
                    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                        raise ValueError("Runtime request exceeds limit")
                    request = json.loads(raw)
                    with serial:
                        value = dispatch(runtime, request["operation"], request["arguments"])
                    response = {"value": value}
                except (ValueError, TypeError, KeyError, RuntimeError, OSError) as exc:
                    response = {"error": str(exc)}
                self.wfile.write(json.dumps(response).encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)


class RuntimeProxy:
    def __init__(self, state_dir):
        self.path = socket_path(state_dir)

    def call(self, operation, arguments):
        request = json.dumps({"operation": operation, "arguments": arguments}).encode() + b"\n"
        if len(request) > MAX_REQUEST:
            raise ValueError("Runtime request exceeds limit")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(40)
            try:
                connection.connect(str(self.path))
            except OSError as exc:
                raise RuntimeError(
                    "Headless worker is not running; start 'flotilla client run'"
                ) from exc
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
                raise RuntimeError("Invalid runtime response")
            response = json.loads(raw)
            if "error" in response:
                raise RuntimeError(response["error"])
            return response["value"]

    def probe(self):
        return self.call("probe", {})

    def sessions(self):
        return self.call("sessions", {})

    def describe(self, session_id):
        return self.call("describe", {"session_id": session_id})

    def create(self, command_id, spec):
        return self.call("create", {"command_id": command_id, "spec": spec})

    def submit(self, command_id, session_id, message_id, run_id, prompt, options):
        return self.call(
            "submit",
            {
                "command_id": command_id,
                "session_id": session_id,
                "message_id": message_id,
                "run_id": run_id,
                "prompt": prompt,
                "options": options,
            },
        )

    def observe(self, session_id, after=0, **query):
        return self.call("observe", {"session_id": session_id, "after": after, **query})

    def interrupt(self, command_id, session_id):
        return self.call("interrupt", {"command_id": command_id, "session_id": session_id})

    def reveal(self, command_id, session_id):
        return self.call("reveal", {"command_id": command_id, "session_id": session_id})

    def close_session(self, command_id, session_id):
        return self.call("close_session", {"command_id": command_id, "session_id": session_id})

    def respond(self, command_id, session_id, question_id, response):
        return self.call(
            "respond",
            {
                "command_id": command_id,
                "session_id": session_id,
                "question_id": question_id,
                "response": response,
            },
        )
