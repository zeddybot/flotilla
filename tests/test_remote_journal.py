import pytest

from slack_conductor.remote_journal import Receipts


def test_receipt_reopens_and_rejects_identity_reuse(tmp_path):
    path = tmp_path / "remote.sqlite"
    original = Receipts(path)
    assert original.begin("send", "native.send", {"prompt": "hello"}) is None
    original.finish("send", {"retry": True})
    assert Receipts(path).begin("send", "native.send", {"prompt": "hello"}) is None
    original.finish("send", {"value": "accepted"})
    assert Receipts(path).begin("send", "native.send", {"prompt": "hello"}) == {"value": "accepted"}
    with pytest.raises(ValueError, match="different arguments"):
        original.begin("send", "native.send", {"prompt": "different"})


def test_client_bindings_survive_server_unavailability(tmp_path):
    path = tmp_path / "remote.sqlite"
    Receipts(path).bind({"session": "workspace"})
    assert Receipts(path).bindings() == {"session": "workspace"}
