"""Move durable Slack server state between hosts while both daemons are stopped.

The archive deliberately excludes native.sqlite and all Conductor state. It is
private because the bridge database may contain Slack message text and signed
upload URLs.
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

DATABASES = ("bridge.sqlite", "remote.sqlite")
MAX_ARCHIVE_FILE = 512 * 1024 * 1024


@contextmanager
def inactive(state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"daemon is active in {state_dir}") from None
        yield


def backup(source, destination):
    with (
        closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as copy,
    ):
        original.backup(copy)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_members(database, state_dir):
    """Locate only snapshots still referenced by durable upload plans."""
    uploads = (state_dir / "uploads").resolve()
    records = []
    with closing(sqlite3.connect(database)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(statuses)")}
        if "attachments" not in columns:
            return records
        for status_id, state, raw in conn.execute("SELECT id,state,attachments FROM statuses"):
            plan = json.loads(raw or "{}")
            if state != "delivering":
                # Completed replies have already deleted their upload snapshots.
                continue
            for index, item in enumerate(plan.get("files", [])):
                if not item.get("snapshot"):
                    continue
                source = Path(item["snapshot"])
                expected = uploads / str(status_id) / str(index)
                if source.resolve() != expected or not source.is_file() or source.is_symlink():
                    raise ValueError(
                        f"unsafe or missing snapshot for status {status_id}, file {index}"
                    )
                member = f"uploads/{status_id}/{index}"
                records.append(
                    {
                        "status_id": status_id,
                        "index": index,
                        "member": member,
                        "sha256": file_digest(source),
                        "size": source.stat().st_size,
                    }
                )
    return records


def add_file(archive, source, name):
    info = archive.gettarinfo(str(source), arcname=name)
    info.mode = 0o600
    with source.open("rb") as data:
        archive.addfile(info, data)


def export_state(state_dir, output):
    state_dir, output = Path(state_dir).resolve(), Path(output).resolve()
    with inactive(state_dir):
        if not (state_dir / "bridge.sqlite").is_file():
            raise FileNotFoundError(state_dir / "bridge.sqlite")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="state-export-") as work:
            work = Path(work)
            files = {}
            for name in DATABASES:
                if (state_dir / name).is_file():
                    backup(state_dir / name, work / name)
                    files[name] = {
                        "size": (work / name).stat().st_size,
                        "sha256": file_digest(work / name),
                    }
            snapshots = snapshot_members(work / "bridge.sqlite", state_dir)
            manifest = {"version": 1, "databases": files, "snapshots": snapshots}
            fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tar.gz", dir=output.parent)
            os.close(fd)
            try:
                with tarfile.open(temporary, "w:gz") as archive:
                    body = json.dumps(manifest, sort_keys=True).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode = len(body), 0o600
                    archive.addfile(info, io.BytesIO(body))
                    for name in files:
                        add_file(archive, work / name, name)
                    for snapshot in snapshots:
                        add_file(archive, state_dir / snapshot["member"], snapshot["member"])
                os.chmod(temporary, 0o600)
                os.replace(temporary, output)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return output


def validate_member(member):
    path = PurePosixPath(member.name)
    if (
        not member.isfile()
        or member.size > MAX_ARCHIVE_FILE
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != member.name
    ):
        raise ValueError("unsafe archive member")


def import_state(archive_path, state_dir, *, replace=False):
    state_dir = Path(state_dir).resolve()
    with (
        inactive(state_dir),
        tempfile.TemporaryDirectory(prefix="state-import-", dir=state_dir) as temporary,
    ):
        work = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            for member in members:
                validate_member(member)
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("duplicate or missing archive member")
            manifest_member = archive.extractfile("manifest.json")
            manifest = json.load(manifest_member)
            if manifest.get("version") != 1:
                raise ValueError("unsupported archive version")
            databases = manifest.get("databases", {})
            snapshots = manifest.get("snapshots", [])
            if "bridge.sqlite" not in databases or set(databases) - set(DATABASES):
                raise ValueError("invalid database manifest")
            expected = {"manifest.json", *databases, *(item["member"] for item in snapshots)}
            if set(names) != expected:
                raise ValueError("unexpected archive member")
            for name in databases:
                destination = work / name
                with archive.extractfile(name) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                if destination.stat().st_size != databases[name]["size"] or (
                    file_digest(destination) != databases[name]["sha256"]
                ):
                    raise ValueError("database checksum mismatch")
                try:
                    with closing(sqlite3.connect(f"file:{destination}?mode=ro", uri=True)) as conn:
                        valid = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                except sqlite3.DatabaseError as exc:
                    raise ValueError("database integrity check failed") from exc
                if not valid:
                    raise ValueError("database integrity check failed")
            mapping = {}
            for item in snapshots:
                name = item["member"]
                expected_name = f"uploads/{item['status_id']}/{item['index']}"
                if name != expected_name or name in mapping:
                    raise ValueError("invalid snapshot manifest")
                destination = work / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(name) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                if destination.stat().st_size != item["size"] or (
                    file_digest(destination) != item["sha256"]
                ):
                    raise ValueError("snapshot checksum mismatch")
                mapping[name] = destination
        with closing(sqlite3.connect(work / "bridge.sqlite")) as conn, conn:
            for status_id, state, raw in conn.execute("SELECT id,state,attachments FROM statuses"):
                plan = json.loads(raw or "{}")
                changed = False
                for index, item in enumerate(plan.get("files", [])):
                    if item.get("snapshot"):
                        if state != "delivering":
                            item.pop("snapshot")
                            changed = True
                            continue
                        member = f"uploads/{status_id}/{index}"
                        if member not in mapping:
                            raise ValueError("missing referenced snapshot")
                        item["snapshot"] = str(state_dir / member)
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE statuses SET attachments=? WHERE id=?",
                        (json.dumps(plan), status_id),
                    )
        for name in databases:
            target = state_dir / name
            if target.exists() and not replace:
                raise FileExistsError(f"{target} exists; pass --replace after stopping daemon")
            if (state_dir / "uploads").exists() and not replace:
                raise FileExistsError("uploads exists; pass --replace after stopping daemon")
        # Preserve previous state for an immediate manual rollback. Native state is untouched.
        previous = state_dir / "migration-previous"
        if replace and previous.exists():
            raise FileExistsError("migration-previous exists; move it before another replace")
        if replace:
            previous.mkdir(mode=0o700)
            for name in (
                *DATABASES,
                "uploads",
                *(f"{db}-{suffix}" for db in DATABASES for suffix in ("wal", "shm")),
            ):
                old = state_dir / name
                if old.exists():
                    os.replace(old, previous / name)
        for name in databases:
            os.chmod(work / name, 0o600)
            os.replace(work / name, state_dir / name)
        if mapping:
            os.replace(work / "uploads", state_dir / "uploads")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--state-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("import")
    restore.add_argument("--state-dir", type=Path, required=True)
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        print(export_state(args.state_dir, args.output))
    else:
        import_state(args.archive, args.state_dir, replace=args.replace)


if __name__ == "__main__":
    main()
