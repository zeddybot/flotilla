import fcntl
import io
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from scripts.migrate_state import export_state, import_state
from slack_conductor.store import Store


def populated_state(root):
    root.mkdir()
    store = Store(root / "bridge.sqlite")
    status_id = store.add_status("event", "channel", "thread", "prompt", "{}")
    snapshot = root / "uploads" / str(status_id) / "0"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"half-uploaded image")
    store.update(
        status_id,
        state="delivering",
        attachments=json.dumps(
            {
                "files": [
                    {
                        "snapshot": str(snapshot),
                        "phase": "bytes",
                        "file_id": "F123",
                        "upload_url": "https://example.invalid/upload",
                        "length": snapshot.stat().st_size,
                    }
                ]
            }
        ),
    )
    with closing(sqlite3.connect(root / "remote.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE receipts (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO receipts VALUES ('accepted')")
    return status_id


def test_round_trip_preserves_upload_recovery_and_native_state(tmp_path):
    source, destination = tmp_path / "mac", tmp_path / "server"
    status_id = populated_state(source)
    archive = export_state(source, tmp_path / "transfer.tar.gz")
    assert archive.stat().st_mode & 0o077 == 0
    destination.mkdir()
    (destination / "native.sqlite").write_bytes(b"native remains on Mac")
    import_state(archive, destination)

    with closing(sqlite3.connect(destination / "bridge.sqlite")) as conn:
        raw = conn.execute("SELECT attachments FROM statuses WHERE id=?", (status_id,)).fetchone()[
            0
        ]
    file = json.loads(raw)["files"][0]
    assert file["snapshot"] == str(destination / "uploads" / str(status_id) / "0")
    assert Path(file["snapshot"]).read_bytes() == b"half-uploaded image"
    assert file["phase"] == "bytes"
    assert (destination / "native.sqlite").read_bytes() == b"native remains on Mac"
    with closing(sqlite3.connect(destination / "remote.sqlite")) as conn:
        assert conn.execute("SELECT id FROM receipts").fetchone()[0] == "accepted"

    rollback = export_state(destination, tmp_path / "rollback.tar.gz")
    import_state(rollback, source, replace=True)
    assert (source / "migration-previous" / "bridge.sqlite").exists()
    assert (source / "uploads" / str(status_id) / "0").read_bytes() == b"half-uploaded image"


def test_export_and_import_reject_running_daemon(tmp_path):
    source = tmp_path / "source"
    populated_state(source)
    archive = export_state(source, tmp_path / "transfer.tar.gz")
    with (source / "daemon.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="daemon is active"):
            export_state(source, tmp_path / "another.tar.gz")
        with pytest.raises(RuntimeError, match="daemon is active"):
            import_state(archive, source, replace=True)


def test_completed_upload_snapshot_already_cleaned_is_not_required(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    status_id = populated_state(source)
    with closing(sqlite3.connect(source / "bridge.sqlite")) as conn, conn:
        conn.execute("UPDATE statuses SET state='complete' WHERE id=?", (status_id,))
    (source / "uploads" / str(status_id) / "0").unlink()
    archive = export_state(source, tmp_path / "completed.tar.gz")
    import_state(archive, destination)
    with closing(sqlite3.connect(destination / "bridge.sqlite")) as conn:
        plan = json.loads(
            conn.execute("SELECT attachments FROM statuses WHERE id=?", (status_id,)).fetchone()[0]
        )
    assert "snapshot" not in plan["files"][0]
    assert plan["files"][0]["file_id"] == "F123"


def test_server_migration_preserves_incomplete_remote_download(tmp_path):
    source = tmp_path / "server"
    status_id = populated_state(source)
    with closing(sqlite3.connect(source / "bridge.sqlite")) as conn, conn:
        conn.execute(
            "UPDATE statuses SET state='delivering',attachments=? WHERE id=?",
            (json.dumps({"files": [{"remote_snapshot": "digest", "length": 42}]}), status_id),
        )
    archive = export_state(source, tmp_path / "transfer.tar.gz")
    destination = tmp_path / "replacement-server"
    import_state(archive, destination)
    plan = json.loads(Store(destination / "bridge.sqlite").status(status_id)["attachments"])
    assert plan["files"] == [{"remote_snapshot": "digest", "length": 42}]


def test_import_rejects_path_traversal_before_writing_state(tmp_path):
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"bad"
        member = tarfile.TarInfo("../escape")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    target = tmp_path / "target"
    with pytest.raises(ValueError, match="unsafe archive member"):
        import_state(archive, target)
    assert not (tmp_path / "escape").exists()


def test_version_one_archive_remains_importable(tmp_path):
    source = tmp_path / "source"
    populated_state(source)
    current = export_state(source, tmp_path / "current.tar.gz")
    old = tmp_path / "legacy.tar.gz"
    with tarfile.open(current, "r:gz") as original, tarfile.open(old, "w:gz") as legacy:
        for member in original:
            with original.extractfile(member) as stream:
                if member.name == "manifest.json":
                    manifest = json.load(stream)
                    manifest.pop("checkpoints")
                    manifest["version"] = 1
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                    legacy.addfile(member, io.BytesIO(data))
                else:
                    legacy.addfile(member, stream)
    target = tmp_path / "target"
    import_state(old, target)
    assert (target / "uploads" / "1" / "0").read_bytes() == b"half-uploaded image"


def test_shared_server_archive_restores_profiles_routes_and_client_state(tmp_path):
    from hashlib import sha256

    from slack_conductor.pairing import PairingStore
    from slack_conductor.remote_journal import Receipts

    source, target = tmp_path / "source", tmp_path / "destination"
    populated_state(source)
    registry = PairingStore(source / "pairing.sqlite")
    token = "paired-bearer-for-a-client-" + "x" * 32
    code = "a" * 32
    registry.register(code, sha256(token.encode()).hexdigest(), "desktop", "conductor")
    profile = registry.claim(code, "T1", "U1")
    registry.set_default("T1", "U1", "desktop")

    global_store = Store(source / "bridge.sqlite")
    global_store.execute(
        "INSERT INTO slack_routes VALUES (?,?,?,?,?)", ("T1", "C1", "1.00", "U1", profile.id)
    )
    client_scope = Path("clients") / profile.id
    (source / "clients").mkdir()
    status_id = populated_state(source / client_scope)
    checkpoint = '{"1.00": 42.0}'
    (source / client_scope / "intake-threads.json").write_text(checkpoint)
    (source / client_scope / "intake-status.json").write_text('{"ready":true}')
    receipt_dir = source / client_scope / "remote.sqlite"
    receipts = Receipts(receipt_dir)
    receipts.begin("action", "runtime.create", {})
    receipts.finish("action", {"value": "confirmed"})

    archive = export_state(source, tmp_path / "shared.tar.gz")
    import_state(archive, target)

    restored_registry = PairingStore(target / "pairing.sqlite")
    assert restored_registry.authenticate(token) == profile
    assert restored_registry.get_default("T1", "U1") == profile
    assert Store(target / "bridge.sqlite").route("T1", "C1", "1.00")["client_id"] == profile.id
    restored_scope = target / client_scope
    restored_receipts = Receipts(restored_scope / "remote.sqlite")
    assert restored_receipts.begin("action", "runtime.create", {}) == {"value": "confirmed"}
    restored_store = Store(restored_scope / "bridge.sqlite")
    plan = json.loads(restored_store.status(status_id)["attachments"])
    snapshot = restored_scope / "uploads" / str(status_id) / "0"
    assert plan["files"][0]["snapshot"] == str(snapshot)
    assert snapshot.read_bytes() == b"half-uploaded image"
    assert (restored_scope / "intake-threads.json").read_text() == checkpoint
    assert not (restored_scope / "intake-status.json").exists()

    rollback = export_state(target, tmp_path / "rollback.tar.gz")
    import_state(rollback, source, replace=True)
    assert (source / "migration-previous" / client_scope / "bridge.sqlite").is_file()
    assert (source / client_scope / "intake-threads.json").read_text() == checkpoint
