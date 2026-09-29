"""Outbound Mac connection; native credentials and execution never leave this host."""

import json
import logging
import os
import sqlite3
import threading
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .conductor.journal import Unconfirmed
from .remote import MAX_MESSAGE, VERSION, RemoteObservations
from .remote_journal import Receipts

log = logging.getLogger(__name__)
OPERATIONS = {"native.validate", "native.new_workspace", "native.send", "native.archive"}


def write_status(state_dir, **values):
    path = state_dir / "remote-status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": time.time(), **values}))
    temporary.chmod(0o600)
    os.replace(temporary, path)


class Dispatcher:
    def __init__(self, config, conductor):
        from .remote_assets import AssetSource

        self.conductor = conductor
        self.receipts = Receipts(config.state_dir / "remote.sqlite")
        self.assets = AssetSource(config.state_dir, conductor)

    def execute(self, message):
        method, args, key = message["method"], message["args"], message.get("key")
        durable = method in {"native.new_workspace", "native.send", "native.archive"}
        if durable and not isinstance(key, str):
            return {"error": "Mutations require a durable command identity"}
        if key and durable:
            saved = self.receipts.begin(key, method, args)
            if saved is not None:
                return saved
        try:
            if method.startswith("files."):
                value = self.assets.call(method, args)
            elif method in OPERATIONS:
                if not self.conductor.ready():
                    raise Unconfirmed("Conductor is reconnecting")
                value = getattr(self.conductor, method.split(".")[1])(**args)
                if method == "native.new_workspace":
                    self.receipts.bind({value["active_session_id"]: value["id"]})
                if args.get("sid"):
                    self.receipts.bind({args["sid"]: args["wid"]})
            elif method.startswith("db.") and method[3:] in RemoteObservations.METHODS:
                parameters = args["args"]
                bindings = self.receipts.bindings()
                owned = bindings.values() if method == "db.workspace" else bindings
                if not parameters or parameters[0] not in owned:
                    raise ValueError("Observation target is not a bound session")
                value = getattr(self.conductor.db, method[3:])(*parameters)

            else:
                raise ValueError("Unknown remote operation")
            response = {"value": value}
        except (Unconfirmed, sqlite3.OperationalError):
            response = {"retry": True}
        except OSError:
            response = (
                {"error": "File snapshot is unavailable on the Mac"}
                if method.startswith("files.")
                else {"retry": True}
            )
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            response = {"error": str(exc)}
        if durable and key:
            self.receipts.finish(key, response)
        return response


def run_client(config, token, stop, *, conductor=None):
    if conductor is None:
        from .conductor.native import Conductor

        conductor = Conductor(config)
    dispatcher = Dispatcher(config, conductor)
    conductor.start(dispatcher.receipts.bindings())
    delay = 1
    try:
        while not stop.is_set():
            peer = None
            heartbeat_stop = threading.Event()
            heartbeat = None
            try:
                with connect(
                    config.server_url,
                    additional_headers={
                        "Authorization": "Bearer " + token,
                        "X-Client-ID": config.client_id,
                    },
                    max_size=MAX_MESSAGE,
                    open_timeout=10,
                    close_timeout=2,
                    ping_interval=15,
                    ping_timeout=20,
                ) as peer:
                    hello = json.loads(peer.recv(timeout=10))
                    if hello.get("version") != VERSION:
                        raise ValueError("Server protocol is incompatible")
                    dispatcher.receipts.bind(hello["bindings"])
                    conductor.transport.bindings.update(hello["bindings"])

                    def pulse(peer=peer, heartbeat_stop=heartbeat_stop):
                        while not heartbeat_stop.is_set():
                            try:
                                ready = conductor.transport.online()
                                peer.send(
                                    json.dumps(
                                        {"version": VERSION, "type": "ready", "ready": ready}
                                    )
                                )
                                write_status(
                                    config.state_dir,
                                    connected=True,
                                    ready=ready,
                                    client_id=config.client_id,
                                )
                            except (WebSocketException, OSError):
                                break
                            if stop.is_set():
                                peer.close(1001, "Client stopping")
                                break
                            heartbeat_stop.wait(1)

                    heartbeat = threading.Thread(target=pulse, daemon=True)
                    heartbeat.start()
                    delay = 1
                    while not stop.is_set():
                        conductor.ready()
                        try:
                            raw = peer.recv(timeout=1)
                        except TimeoutError:
                            continue
                        message = json.loads(raw)
                        if message.get("version") != VERSION or message.get("type") != "request":
                            raise ValueError("Invalid server request")
                        response = dispatcher.execute(message)
                        peer.send(
                            json.dumps(
                                {
                                    **response,
                                    "version": VERSION,
                                    "type": "response",
                                    "id": message["id"],
                                }
                            )
                        )
            except (WebSocketException, OSError, ValueError, KeyError, Unconfirmed) as exc:
                # Authenticated preview URLs and prompts must not appear in diagnostics.
                log.warning("Server connection unavailable (%s)", type(exc).__name__)
            finally:
                heartbeat_stop.set()
                if heartbeat:
                    heartbeat.join(timeout=3)
                write_status(
                    config.state_dir, connected=False, ready=False, client_id=config.client_id
                )
            if stop.wait(delay):
                break
            delay = min(delay * 2, 30)
    finally:
        conductor.close()
