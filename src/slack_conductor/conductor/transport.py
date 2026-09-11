"""Authenticated loopback ingress for the attached Conductor frontend."""

import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .journal import Journal


class Transport:
    def __init__(self, state_dir, alive):
        self.journal = Journal(state_dir / "native.sqlite")
        self.alive = alive
        self.lock = threading.RLock()
        self.attachment = None
        self.heartbeat = {}
        self.bindings = {}
        self.bootstrap = ""
        self.token = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, code=200, script=False):
                data = (value if script else json.dumps(value)).encode()
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", "tauri://localhost")
                self.send_header(
                    "Content-Type", "application/javascript" if script else "application/json"
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "tauri://localhost")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def handle_request(self, post=False):
                url = urlparse(self.path)
                prefix = "/" + owner.token + "/"
                if not url.path.startswith(prefix):
                    self.reply(False, 404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 2_000_000:
                        raise ValueError("Request too large")
                    body = json.loads(self.rfile.read(size)) if post else {}
                    generation = (
                        body.get("generation")
                        if post
                        else parse_qs(url.query).get("generation", [""])[0]
                    )
                    with owner.lock:
                        if not owner.valid(generation):
                            self.reply(False, 409)
                            return
                        route = url.path[len(prefix) :]
                        if route == "bootstrap" and not post and owner.bootstrap:
                            self.reply(owner.bootstrap, script=True)
                            return
                        elif route == "next" and not post:
                            result = owner.journal.claim(generation)
                            if result and result["body"].get("sid") and result["body"].get("wid"):
                                owner.bindings[result["body"]["sid"]] = result["body"]["wid"]
                        elif route == "valid" and not post:
                            result = True
                        elif route == "heartbeat" and post:
                            owner.heartbeat = {**body, "received_at": time.monotonic()}
                            with tempfile.NamedTemporaryFile(
                                mode="w", dir=state_dir, delete=False
                            ) as f:
                                json.dump(
                                    {
                                        **body,
                                        "at": time.time(),
                                        "incarnation": owner.attachment["incarnation"],
                                    },
                                    f,
                                )
                            os.replace(f.name, state_dir / "native-heartbeat.json")
                            result = True
                        elif route == "result" and post:
                            result = owner.journal.complete(body["id"], generation, body)
                        elif route == "pause" and post and body["sid"] in owner.bindings:
                            result = owner.journal.pause(
                                body["request"], body["sid"], body["paused"]
                            )
                        elif route == "pause" and not post:
                            sid = parse_qs(url.query).get("sid", [""])[0]
                            result = owner.journal.pause_state(sid)
                        else:
                            self.reply(False, 404)
                            return
                    self.reply(result)
                except (ValueError, KeyError, TypeError):
                    self.reply(False, 400)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.worker.start()

    def attach(self, incarnation):
        with self.lock:
            self.attachment = {"generation": str(uuid.uuid4()), "incarnation": incarnation}
            self.heartbeat = {}
            self.bootstrap = ""
            return {
                **self.attachment,
                "base": f"http://127.0.0.1:{self.server.server_port}/{self.token}",
                "bindings": dict(self.bindings),
            }

    def valid(self, generation):
        return bool(
            self.attachment
            and generation == self.attachment["generation"]
            and self.alive(self.attachment["incarnation"])
        )

    def online(self):
        with self.lock:
            return bool(
                self.attachment
                and self.valid(self.attachment["generation"])
                and self.heartbeat.get("ready")
                and time.monotonic() - self.heartbeat.get("received_at", 0) < 5
            )

    def session(self, sid):
        if not self.online():
            return None
        value = self.heartbeat.get("sessions", {}).get(sid)
        if value and time.time() - value.get("at", 0) < 5:
            return {**value, "incarnation": self.attachment["incarnation"]}
        return None

    def call(self, body, key=None, timeout=30):
        key = key or str(uuid.uuid4())
        with self.lock:
            if body["op"] == "detach":
                if not self.attachment:
                    return True
                body = {**body, "target_generation": self.attachment["generation"]}
            self.journal.enqueue(key, body)
        return self.journal.wait(key, timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
