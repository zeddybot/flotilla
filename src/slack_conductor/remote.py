"""Versioned session-runtime transport; Slack and delivery state stay on the server."""

import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from http import HTTPStatus

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from .conductor.journal import Unconfirmed
from .remote_journal import Receipts
from .runtime import MUTATIONS, RUNTIME_VERSION
from .runtime_observations import RuntimeObservations

log = logging.getLogger(__name__)
VERSION = 2
MAX_MESSAGE = 64 * 1024 * 1024


def command_key(method, args):
    value = json.dumps([method, args], sort_keys=True).encode()
    return hashlib.sha256(value).hexdigest()


def unpack(response):
    if response.get("retry"):
        raise Unconfirmed("Mac client is reconnecting; your message is saved")
    if "error" in response:
        raise RuntimeError(response["error"])
    return response.get("value")


class RemoteConductor:
    allow_offline = True

    def __init__(self, config, token):
        self.config, self.token = config, token
        self.receipts = Receipts(config.state_dir / "remote.sqlite")
        self.db = RuntimeObservations(self)
        self.lock = threading.RLock()
        self.serial = threading.Lock()
        self.peer = None
        self.pending = {}
        self.native_ready = False
        self.last_ready = 0
        self.server = self.worker = None
        self.bindings = {}
        self.slack_ready = None

    def authorize(self, connection, request):
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.token
        ):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        if request.path == "/status":
            from dataclasses import replace

            from .monitoring import report

            snapshot = report(
                replace(self.config, mode="server"),
                runtime_ready=self.ready(),
                slack_ready=self.slack_ready,
            )
            return connection.respond(HTTPStatus.OK, json.dumps(snapshot))
        if request.path == "/health":
            return connection.respond(HTTPStatus.OK, json.dumps({"ready": self.ready()}))
        if request.path != "/client" or request.headers.get("X-Client-ID") != self.config.client_id:
            return connection.respond(HTTPStatus.FORBIDDEN, "Unknown client\n")

    def start(self, bindings=None):
        self.bindings.update(bindings or {})
        self.server = serve(
            self.handle,
            self.config.server_host,
            self.config.server_port,
            process_request=self.authorize,
            origins=[None],
            max_size=MAX_MESSAGE,
            ping_interval=15,
            ping_timeout=20,
            close_timeout=2,
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def handle(self, peer):
        with self.lock:
            if self.peer is not None:
                peer.close(1008, "Client already connected")
                return
            self.peer = peer
            self.native_ready = False
        try:
            peer.send(
                json.dumps(
                    {
                        "version": VERSION,
                        "runtime_version": RUNTIME_VERSION,
                        "bindings": self.bindings,
                    }
                )
            )
            for raw in peer:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("version") != VERSION:
                    peer.close(1008, "Unsupported protocol")
                    break
                with self.lock:
                    if self.peer is not peer:
                        break
                    if message.get("type") == "ready":
                        self.native_ready = message.get("ready") is True
                        self.last_ready = time.monotonic()
                    elif message.get("type") == "response":
                        waiter = self.pending.get(message.get("id"))
                        if waiter and waiter[0] is peer:
                            waiter[2].update(message)
                            waiter[1].set()
        except (ConnectionClosed, ValueError, TypeError):
            log.info("Mac connection closed")
        finally:
            with self.lock:
                if self.peer is peer:
                    self.peer = None
                    self.native_ready = False
                for owner, event, response in self.pending.values():
                    if owner is peer:
                        response.update(retry=True)
                        event.set()

    def ready(self):
        with self.lock:
            return bool(self.peer and self.native_ready and time.monotonic() - self.last_ready < 45)

    def rpc(self, method, args, key=None, timeout=40):
        # One caller controls a session at a time, matching the local service loop.
        durable = method.removeprefix("runtime.") in MUTATIONS
        with self.serial:
            if key and durable:
                saved = self.receipts.begin(key, method, args)
                if saved is not None:
                    return unpack(saved)
            request_id = str(uuid.uuid4())
            event, response = threading.Event(), {}
            with self.lock:
                peer = self.peer
                if not peer or not self.ready():
                    raise Unconfirmed("Mac client is offline; your message is saved")
                self.pending[request_id] = (peer, event, response)
            try:
                peer.send(
                    json.dumps(
                        {
                            "version": VERSION,
                            "type": "request",
                            "id": request_id,
                            "key": key,
                            "method": method,
                            "args": args,
                        }
                    )
                )
                if not event.wait(timeout):
                    # Retire the connection: subsequent observations must not pass a timed-out action.
                    peer.close(1011, "Operation timed out")
                    raise Unconfirmed("Mac response unconfirmed; checking the saved operation")
                if key and durable:
                    self.receipts.finish(key, response)
                return unpack(response)
            except (ConnectionClosed, OSError):
                raise Unconfirmed("Mac connection interrupted; your message is saved") from None
            finally:
                with self.lock:
                    self.pending.pop(request_id, None)

    def probe(self):
        return self.rpc("runtime.probe", {})

    def validate(self, wid, sid=None):
        value = self.rpc("runtime.recover", {"workspace_id": wid, "session_id": sid})
        self.bindings[value["session_id"]] = value["workspace_id"]
        return {"id": value["workspace_id"], "active_session_id": value["session_id"]}

    def new_workspace(self, key=None, spec=None):
        key = key or str(uuid.uuid4())
        command = "runtime:create:" + key
        value = self.rpc("runtime.create", {"command_id": key, "spec": spec or {}}, key=command)
        self.bindings[value["session_id"]] = value["workspace_id"]
        return {"id": value["workspace_id"], "active_session_id": value["session_id"]}

    def create_session(self, key, spec):
        return self.new_workspace(key, spec)

    def send(self, wid, sid, prompt, options):
        self.bindings[sid] = wid
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, sid + ":" + prompt))
        command = "runtime:submit:" + message_id
        value = self.rpc(
            "runtime.submit",
            {
                "command_id": command,
                "session_id": sid,
                "message_id": message_id,
                "run_id": "run:" + message_id,
                "prompt": prompt,
                "options": options,
            },
            key=command,
        )
        return value.get("warning", "")

    def archive(self, wid, sid):
        command = "runtime:close:" + wid
        self.rpc("runtime.close_session", {"command_id": command, "session_id": sid}, key=command)
        return ""

    def make_uploads(self, store, slack, clock):
        from .remote_assets import RemoteUploads

        return RemoteUploads(store, slack, clock=clock, rpc=self.rpc)

    def close(self):
        with self.lock:
            peer = self.peer
        if peer:
            peer.close(1001, "Server stopping")
        if self.server:
            self.server.shutdown()
            self.worker.join(timeout=5)


def run_client(config, token, stop):
    from .remote_client import run_client as run

    return run(config, token, stop)


# Kept for installed entrypoints while callers adopt the runtime name.
RemoteRuntime = RemoteConductor
