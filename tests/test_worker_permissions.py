import pytest

from slack_conductor.config import Config
from slack_conductor.headless.engines import command
from slack_conductor.headless.permissions import within_policy


def test_external_boundary_requires_explicit_isolated_host(monkeypatch):
    monkeypatch.delenv("FLOTILLA_ISOLATED_WORKER", raising=False)
    config = Config(engine_permission_policy="external-sandbox")
    with pytest.raises(ValueError, match="isolated Linux"):
        command(config, "codex", None, "hello")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("FLOTILLA_ISOLATED_WORKER", "1")
    assert "danger-full-access" in command(config, "codex", None, "hello")
    assert "bypassPermissions" in command(config, "claude", "sid", "hello")


@pytest.mark.parametrize(
    "requested,configured",
    [("workspace-write", "read-only"), ("external-sandbox", "workspace-write")],
)
def test_remote_spec_cannot_broaden_worker_policy(monkeypatch, requested, configured):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("FLOTILLA_ISOLATED_WORKER", "1")
    with pytest.raises(ValueError, match="exceeds"):
        within_policy(requested, configured)
