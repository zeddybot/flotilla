import pytest

from slack_conductor.config import Config
from slack_conductor.headless.engines import codex_thread_options, command
from slack_conductor.headless.permissions import within_policy


def test_external_boundary_requires_explicit_isolated_host(monkeypatch):
    monkeypatch.delenv("FLOTILLA_ISOLATED_WORKER", raising=False)
    config = Config(engine_permission_policy="external-sandbox")
    with pytest.raises(ValueError, match="isolated Linux"):
        command(config, "codex", None)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("FLOTILLA_ISOLATED_WORKER", "1")
    assert command(config, "codex", None)[1:] == ["app-server", "--listen", "stdio://"]
    assert (
        codex_thread_options(
            {"permission_policy": "external-sandbox", "model": None, "effort": None}, "/work"
        )["sandbox"]
        == "danger-full-access"
    )
    assert "bypassPermissions" in command(config, "claude", "sid")


@pytest.mark.parametrize(
    "requested,configured",
    [("workspace-write", "read-only"), ("external-sandbox", "workspace-write")],
)
def test_remote_spec_cannot_broaden_worker_policy(monkeypatch, requested, configured):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("FLOTILLA_ISOLATED_WORKER", "1")
    with pytest.raises(ValueError, match="exceeds"):
        within_policy(requested, configured)
