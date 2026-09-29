import pytest

from slack_conductor.runtime_local import RuntimeProxy, RuntimeServer


def test_cli_uses_service_owned_runtime_and_propagates_errors(tmp_path):
    class Runtime:
        def probe(self):
            return {"adapter": "headless", "ready": True}

        def create(self, command_id, spec):
            return {"session_id": command_id, "spec": spec}

        def interrupt(self, command_id, session_id):
            raise ValueError("Session is not owned by this worker")

    server = RuntimeServer(tmp_path, Runtime())
    server.start()
    proxy = RuntimeProxy(tmp_path)
    try:
        assert proxy.probe()["ready"]
        assert proxy.create("create", {"engine": "codex"})["session_id"] == "create"
        assert server.path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(RuntimeError, match="not owned"):
            proxy.interrupt("stop", "foreign")
    finally:
        server.close()
    with pytest.raises(RuntimeError, match="not running"):
        proxy.probe()


def test_oversized_local_command_is_rejected_before_connect(tmp_path):
    with pytest.raises(ValueError, match="exceeds"):
        RuntimeProxy(tmp_path).create("large", {"value": "x" * (3 * 1024 * 1024)})
