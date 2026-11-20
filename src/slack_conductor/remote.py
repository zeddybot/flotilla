"""Authenticate clients and serve their isolated session-runtime endpoints."""

import hashlib
import json
import secrets
import threading
from dataclasses import replace
from http import HTTPStatus

from websockets.datastructures import MultipleValuesError
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from . import remote_peer
from .pairing import PairingStore
from .store import Store

VERSION = remote_peer.VERSION

MAX_MESSAGE = 64 * 1024 * 1024


def command_key(method, args):
    value = json.dumps([method, args], sort_keys=True).encode()
    return hashlib.sha256(value).hexdigest()


class RemoteConductor(remote_peer.RemotePeer):
    """Host the legacy endpoint and one endpoint per registered profile."""

    def __init__(self, config, token):
        self.stopping = threading.Event()
        super().__init__(config, enabled=lambda: not self.stopping.is_set())
        self.token = token
        self.pairing = PairingStore(config.state_dir / "pairing.sqlite")
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.server = self.worker = self.revocation_worker = None
        self.slack_ready = None

    def for_client(self, client_id):
        if self.stopping.is_set():
            raise ValueError("Server is stopping")
        if client_id == self.client_id:
            return self
        with self.clients_lock:
            if self.stopping.is_set():
                raise ValueError("Server is stopping")
            profile = self.pairing.get(client_id)
            if profile is None:
                raise ValueError("Unknown client")
            endpoint = self.clients.get(client_id)
            if endpoint is None:
                state_dir = self.config.state_dir / "clients" / profile.id
                state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                config = replace(
                    self.config,
                    state_dir=state_dir,
                    client_id=profile.id,
                    runtime_adapter=profile.backend,
                )
                endpoint = remote_peer.RemotePeer(
                    config,
                    enabled=lambda: (
                        not self.stopping.is_set() and self.pairing.is_active(profile.id)
                    ),
                )
                endpoint.bindings.update(Store(state_dir / "bridge.sqlite").session_bindings())
                self.clients[client_id] = endpoint
            return endpoint

    def authorize(self, connection, request):
        try:
            return self._authorize(connection, request)
        except MultipleValuesError:
            return connection.respond(HTTPStatus.BAD_REQUEST, "Duplicate authentication header\n")

    def _authorize(self, connection, request):
        auth = request.headers.get("Authorization", "")
        if request.path == "/pair":
            # Validate the WebSocket upgrade before spending a one-time registration code.
            connection.pair_registration = (
                request.headers.get("X-Flotilla-Pair-Code", ""),
                request.headers.get("X-Flotilla-Token-SHA256", ""),
                request.headers.get("X-Flotilla-Profile", ""),
                request.headers.get("X-Flotilla-Backend", ""),
            )
            return None
        if request.path == "/pair/check":
            connection.pair_profile = self._paired_profile(auth)
            return None
        if secrets.compare_digest(auth.encode(), ("Bearer " + self.token).encode()):
            if request.path == "/status":
                from .monitoring import report

                snapshot = report(
                    replace(self.config, mode="server"),
                    runtime_ready=self.ready(),
                    slack_ready=self.slack_ready,
                )
                return connection.respond(HTTPStatus.OK, json.dumps(snapshot))
            if request.path == "/health":
                return connection.respond(HTTPStatus.OK, json.dumps({"ready": self.ready()}))
            if request.path == "/client" and request.headers.get("X-Client-ID") == self.client_id:
                connection.client_endpoint = self
                return None
            return connection.respond(HTTPStatus.FORBIDDEN, "Unknown client\n")
        if request.path in {"/client", "/health"}:
            profile = self._paired_profile(auth)
            if profile is not None and profile.id != self.client_id:
                if request.path == "/health":
                    with self.clients_lock:
                        endpoint = self.clients.get(profile.id)
                    ready = endpoint is not None and endpoint.ready()
                    return connection.respond(HTTPStatus.OK, json.dumps({"ready": ready}))
                # The paired credential selects the server-issued identity.
                try:
                    connection.client_endpoint = self.for_client(profile.id)
                except ValueError:
                    return connection.respond(HTTPStatus.FORBIDDEN, "Unknown client\n")
                return None
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    def _paired_profile(self, auth):
        scheme, _, token = auth.partition(" ")
        if scheme == "Bearer" and token:
            return self.pairing.authenticate(token)
        return None

    def _handle_connection(self, peer):
        try:
            if peer.request.path == "/pair":
                try:
                    self.pairing.register(*peer.pair_registration)
                except ValueError:
                    peer.send(json.dumps({"ok": False}))
                else:
                    peer.send(json.dumps({"ok": True}))
            elif peer.request.path == "/pair/check":
                profile = peer.pair_profile
                if profile is not None and self.pairing.is_active(profile.id):
                    peer.send(json.dumps({"ok": True, "profile_id": profile.id}))
                else:
                    peer.send(json.dumps({"ok": False}))
            else:
                peer.client_endpoint.handle(peer)
        except ConnectionClosed:
            pass

    def _watch_revocations(self):
        while not self.stopping.wait(1):
            with self.clients_lock:
                endpoints = list(self.clients.values())
            for endpoint in endpoints:
                with endpoint.lock:
                    connected = endpoint.peer is not None
                if connected and not endpoint.enabled():
                    endpoint.close(1008, "Client access revoked")

    def start(self, bindings=None):
        self.bindings.update(bindings or {})
        self.server = serve(
            self._handle_connection,
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
        self.revocation_worker = threading.Thread(target=self._watch_revocations, daemon=True)
        self.revocation_worker.start()

    def close(self):
        self.stopping.set()
        if self.server:
            self.server.shutdown()
        with self.clients_lock:
            endpoints = list(self.clients.values())
        super().close()
        for endpoint in endpoints:
            endpoint.close()
        if self.worker:
            self.worker.join(timeout=5)
            self.revocation_worker.join(timeout=5)


def run_client(config, token, stop):
    from .remote_client import run_client as run

    return run(config, token, stop)


# Kept for installed entrypoints while callers adopt the runtime name.
RemoteRuntime = RemoteConductor
