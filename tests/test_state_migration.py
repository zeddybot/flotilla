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
