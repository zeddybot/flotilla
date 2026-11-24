"""Isolated session-runtime connection and durable receipts for one client."""

import json
import logging
import threading
import time
import uuid

from websockets.exceptions import ConnectionClosed

from .conductor.journal import Unconfirmed
from .remote_journal import Receipts
from .runtime import MUTATIONS, RUNTIME_VERSION
from .runtime_observations import RuntimeObservations

log = logging.getLogger(__name__)
VERSION = 2
LEGACY_STEERING_REJECTION = "The headless turn is not ready for steering"


class SubmissionRejected(RuntimeError):
    """The client definitively rejected the prompt before native pickup."""

    note_prefix = "submission rejected: "


def submission_id(sid, prompt):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, sid + ":" + prompt))


def unpack(response):
    if response.get("retry"):
        raise Unconfirmed("Mac client is reconnecting; your message is saved")
    if "error" in response:
        if response.get("error_type") == "submission_rejected":
            raise SubmissionRejected(response["error"])
        raise RuntimeError(response["error"])
    return response.get("value")


class RemotePeer:
    allow_offline = True

    def __init__(self, config, *, enabled=None):
        self.config = config
        self.client_id = config.client_id
        self.backend = config.runtime_adapter
        self.enabled = enabled or (lambda: True)
        self.receipts = Receipts(config.state_dir / "remote.sqlite")
        self.db = RuntimeObservations(self)
        self.lock = threading.RLock()
        self.serial = threading.Lock()
        self.peer = None
        self.pending = {}
        self.native_ready = False
        self.last_ready = 0
        self.bindings = {}

    def handle(self, peer):
        with self.lock:
            if not self.enabled():
                denial = "Client access revoked"
            elif self.peer is not None:
                denial = "Client already connected"
            else:
                denial = None
                self.peer = peer
                self.native_ready = False
                bindings = self.bindings.copy()
        if denial:
            peer.close(1008, denial)
            return
        try:
            peer.send(
                json.dumps(
                    {
                        "version": VERSION,
                        "runtime_version": RUNTIME_VERSION,
                        "bindings": bindings,
                    }
                )
            )
            for raw in peer:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("version") != VERSION:
                    peer.close(1008, "Unsupported protocol")
                    break
                if not self.enabled():
                    peer.close(1008, "Client access revoked")
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
                    if owner is peer and not event.is_set():
                        response.update(retry=True)
                        event.set()

    def ready(self):
        with self.lock:
            return bool(
                self.peer
                and self.native_ready
                and time.monotonic() - self.last_ready < 45
                and self.enabled()
            )

    def rpc(self, method, args, key=None, timeout=40):
        # One caller controls a session at a time, matching the local service loop.
        durable = method.removeprefix("runtime.") in MUTATIONS
        with self.serial:
            if not self.enabled():
                raise Unconfirmed("Client access revoked")
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
                if not self.enabled():
                    raise Unconfirmed("Client access revoked")
                if key and durable:
                    self.receipts.finish(key, response)
                return unpack(response)
            except (ConnectionClosed, OSError):
                raise Unconfirmed("Mac connection interrupted; your message is saved") from None
            finally:
                with self.lock:
                    self.pending.pop(request_id, None)

    def session_inventory(self):
        if not self.ready():
            return None
        try:
            return self.rpc("runtime.sessions", {})
        except RuntimeError as exc:
            log.info("Session inventory unavailable (%s)", type(exc).__name__)
            return None

    def probe(self):
        return self.rpc("runtime.probe", {})

    def validate(self, wid, sid=None):
        value = self.rpc("runtime.recover", {"workspace_id": wid, "session_id": sid})
        with self.lock:
            self.bindings[value["session_id"]] = value["workspace_id"]
        return {"id": value["workspace_id"], "active_session_id": value["session_id"]}

    def new_workspace(self, key=None, spec=None):
        key = key or str(uuid.uuid4())
        command = "runtime:create:" + key
        value = self.rpc("runtime.create", {"command_id": key, "spec": spec or {}}, key=command)
        with self.lock:
            self.bindings[value["session_id"]] = value["workspace_id"]
        return {"id": value["workspace_id"], "active_session_id": value["session_id"]}

    def create_session(self, key, spec):
        return self.new_workspace(key, spec)

    def send(self, wid, sid, prompt, options):
        with self.lock:
            self.bindings[sid] = wid
        message_id = submission_id(sid, prompt)
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
        return value.get("warning") or ""

    def submission_rejection(self, sid, prompt, note):
        typed = note.startswith(SubmissionRejected.note_prefix)
        legacy = self.backend == "headless" and note == f"error: {LEGACY_STEERING_REJECTION}"
        if not (typed or legacy):
            return None
        saved = self.receipts.saved("runtime:submit:" + submission_id(sid, prompt))
        if saved is None:
            return None
        request, response = saved
        args = request["args"]
        if (
            request["method"] == "runtime.submit"
            and args["session_id"] == sid
            and args["prompt"] == prompt
            and not response.get("retry")
            and "value" not in response
        ):
            if typed and response.get("error_type") == "submission_rejected":
                return response["error"]
            if (
                legacy
                and args["options"].get("send_mode") == "steer"
                and response.get("error") == LEGACY_STEERING_REJECTION
            ):
                return LEGACY_STEERING_REJECTION
        return None

    def archive(self, wid, sid, *, command_id=None):
        command = (
            "runtime:close:" + wid if command_id is None else "runtime:close-event:" + command_id
        )
        self.rpc("runtime.close_session", {"command_id": command, "session_id": sid}, key=command)
        return ""

    def make_uploads(self, store, slack, clock):
        from .remote_assets import RemoteUploads

        return RemoteUploads(store, slack, clock=clock, rpc=self.rpc)

    def close(self, code=1001, reason="Server stopping"):
        with self.lock:
            peer = self.peer
        if peer:
            peer.close(code, reason)
