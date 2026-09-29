"""Versioned session-runtime contract, independent of host, engine and Slack."""

from typing import Protocol

RUNTIME_VERSION = 1
OPERATIONS = frozenset(
    {
        "probe",
        "sessions",
        "describe",
        "create",
        "recover",
        "submit",
        "observe",
        "close_session",
        "interrupt",
        "reveal",
        "respond",
    }
)
MUTATIONS = frozenset({"create", "submit", "close_session", "interrupt", "reveal", "respond"})


class Unsupported(RuntimeError):
    """The selected runtime cannot provide the requested semantics."""


class SessionRuntime(Protocol):
    """JSON-compatible values cross the boundary; native state stays in the adapter.

    Session/workspace IDs are opaque bindings. A run owns logical work across
    native callback segments; a message belongs to a run, and a command owns
    one retryable operation. None of these identities implies another.
    """

    def start(self, bindings: dict) -> None: ...
    def bind(self, bindings: dict) -> None: ...
    def ready(self) -> bool: ...
    def shutdown(self) -> None: ...
    def files(self, operation: str, arguments: dict) -> dict: ...
    def probe(self) -> dict: ...
    def sessions(self) -> list[dict]: ...
    def describe(self, session_id: str) -> dict: ...
    def create(self, command_id: str, spec: dict) -> dict: ...
    def recover(self, workspace_id: str, session_id: str | None = None) -> dict: ...
    def submit(
        self,
        command_id: str,
        session_id: str,
        message_id: str,
        run_id: str,
        prompt: str,
        options: dict,
    ) -> dict: ...
    def observe(self, session_id: str, after: int = 0, **query) -> dict: ...
    def close_session(self, command_id: str, session_id: str) -> dict: ...
    def interrupt(self, command_id: str, session_id: str) -> dict: ...
    def reveal(self, command_id: str, session_id: str) -> dict: ...
    def respond(
        self, command_id: str, session_id: str, question_id: str, response: dict
    ) -> dict: ...


def dispatch(runtime: SessionRuntime, operation: str, arguments: dict):
    if operation not in OPERATIONS:
        raise Unsupported(f"Unknown runtime operation: {operation}")
    if operation in MUTATIONS and not arguments.get("command_id"):
        raise ValueError("Runtime mutations require a command_id")
    return getattr(runtime, operation)(**arguments)
