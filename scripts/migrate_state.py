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
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

DATABASES = ("bridge.sqlite", "remote.sqlite")
GLOBAL_DATABASES = (*DATABASES, "pairing.sqlite")
CHECKPOINTS = "intake-threads.json"
CLIENT_ID = re.compile(r"[0-9a-f]{32}")
INSTALL_PATHS = (
    *GLOBAL_DATABASES,
    "uploads",
    "clients",
    CHECKPOINTS,
    *(f"{db}-{suffix}" for db in GLOBAL_DATABASES for suffix in ("wal", "shm", "journal")),
)
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
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as copy,
    ):
        original.backup(copy)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_members(database, state_dir, scope):
    """Locate only snapshots still referenced by durable upload plans."""
    uploads = state_dir / scope / "uploads"
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
                if source != expected or source.resolve() != expected or not source.is_file():
                    raise ValueError(
                        f"unsafe or missing snapshot for status {status_id}, file {index}"
                    )
                member = (scope / "uploads" / str(status_id) / str(index)).as_posix()
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
    validate_member(info)
    info.mode = 0o600
    with source.open("rb") as data:
        archive.addfile(info, data)


def state_databases(state_dir):
    for name in GLOBAL_DATABASES:
        if (state_dir / name).is_file():
            yield name
    clients = state_dir / "clients"
    if clients.is_symlink():
        raise ValueError("unsafe client state path")
    if clients.is_dir():
        for client in sorted(clients.iterdir()):
            if CLIENT_ID.fullmatch(client.name) and client.is_dir():
                for name in DATABASES:
                    path = client / name
                    if path.is_file():
                        yield path.relative_to(state_dir).as_posix()


def export_state(state_dir, output):
    state_dir, output = Path(state_dir).resolve(), Path(output).resolve()
    with inactive(state_dir):
        if not (state_dir / "bridge.sqlite").is_file():
            raise FileNotFoundError(state_dir / "bridge.sqlite")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="state-export-") as work:
            work = Path(work)
            files, checkpoints, snapshots = {}, {}, []
            for name in state_databases(state_dir):
                source, destination = state_dir / name, work / name
                if source.resolve() != source:
                    raise ValueError("unsafe database path")
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup(source, destination)
                files[name] = {
                    "size": destination.stat().st_size,
                    "sha256": file_digest(destination),
                }
                if destination.name == "bridge.sqlite":
                    scope = Path(name).parent
                    snapshots.extend(snapshot_members(destination, state_dir, scope))
                    checkpoint = state_dir / scope / CHECKPOINTS
                    if checkpoint.is_file():
                        if checkpoint.resolve() != checkpoint:
                            raise ValueError("unsafe recovery checkpoint path")
                        checkpoints[(scope / CHECKPOINTS).as_posix()] = {
                            "size": checkpoint.stat().st_size,
                            "sha256": file_digest(checkpoint),
                        }
            manifest = {
                "version": 2,
                "databases": files,
                "checkpoints": checkpoints,
                "snapshots": snapshots,
            }
            fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tar.gz", dir=output.parent)
            os.close(fd)
            try:
                with tarfile.open(temporary, "w:gz", dereference=True) as archive:
                    body = json.dumps(manifest, sort_keys=True).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode = len(body), 0o600
                    archive.addfile(info, io.BytesIO(body))
                    for name in files:
                        add_file(archive, work / name, name)
                    for name in checkpoints:
                        add_file(archive, state_dir / name, name)
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


def validate_database_name(name, version):
    if name in DATABASES or version == 2 and name in GLOBAL_DATABASES:
        return
    parts = PurePosixPath(name).parts
    if (
        version != 2
        or len(parts) != 3
        or parts[0] != "clients"
        or not CLIENT_ID.fullmatch(parts[1])
        or parts[2] not in DATABASES
    ):
        raise ValueError("invalid database manifest")


def stage_file(archive, work, name, metadata):
    destination = work / name
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with archive.extractfile(name) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)
    if destination.stat().st_size != metadata["size"] or (
        file_digest(destination) != metadata["sha256"]
    ):
        raise ValueError(f"archive checksum mismatch for {name}")
    destination.chmod(0o600)
    return destination


def stage_archive(archive_path, work):
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        for member in members:
            validate_member(member)
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("duplicate or missing archive member")
        with archive.extractfile("manifest.json") as body:
            manifest = json.load(body)
        version = manifest.get("version")
        if type(version) is not int or version not in (1, 2):
            raise ValueError("unsupported archive version")
        databases = manifest.get("databases", {})
        checkpoints = manifest.get("checkpoints", {})
        snapshots = manifest.get("snapshots", [])
        if "bridge.sqlite" not in databases or (version == 1 and checkpoints):
            raise ValueError("invalid database manifest")
        expected = {
            "manifest.json",
            *databases,
            *checkpoints,
            *(item["member"] for item in snapshots),
        }
        if set(names) != expected:
            raise ValueError("unexpected archive member")
        for name, metadata in databases.items():
            validate_database_name(name, version)
            destination = stage_file(archive, work, name, metadata)
            try:
                with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as conn:
                    valid = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            except sqlite3.DatabaseError as exc:
                raise ValueError("database integrity check failed") from exc
            if not valid:
                raise ValueError("database integrity check failed")
        for name, metadata in checkpoints.items():
            path = PurePosixPath(name)
            if (
                path.name != CHECKPOINTS
                or (path.parent / "bridge.sqlite").as_posix() not in databases
            ):
                raise ValueError("invalid checkpoint manifest")
            destination = stage_file(archive, work, name, metadata)
            with destination.open() as data:
                if not isinstance(json.load(data), dict):
                    raise TypeError("invalid recovery checkpoint")
        snapshot_names = set()
        for item in snapshots:
            name = item["member"]
            path = PurePosixPath(name)
            status_id, index = item["status_id"], item["index"]
            bridge = (path.parent.parent.parent / "bridge.sqlite").as_posix()
            if (
                bridge not in databases
                or type(status_id) is not int
                or type(index) is not int
                or index < 0
                or path.parts[-3:] != ("uploads", str(status_id), str(index))
                or name in snapshot_names
            ):
                raise ValueError("invalid snapshot manifest")
            stage_file(archive, work, name, item)
            snapshot_names.add(name)
    return databases, snapshot_names


def rewrite_snapshots(database, scope, state_dir, remaining):
    with closing(sqlite3.connect(database)) as conn, conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(statuses)")}
        if "attachments" not in columns:
            return
        for status_id, state, raw in conn.execute("SELECT id,state,attachments FROM statuses"):
            plan = json.loads(raw or "{}")
            changed = False
            for index, item in enumerate(plan.get("files", [])):
                if not item.get("snapshot"):
                    continue
                if state != "delivering":
                    item.pop("snapshot")
                else:
                    member = scope / "uploads" / str(status_id) / str(index)
                    if member.as_posix() not in remaining:
                        raise ValueError("missing referenced snapshot")
                    remaining.remove(member.as_posix())
                    item["snapshot"] = str(state_dir / member)
                changed = True
            if changed:
                conn.execute(
                    "UPDATE statuses SET attachments=? WHERE id=?", (json.dumps(plan), status_id)
                )


def install_state(work, state_dir, *, replace):
    incoming = [name for name in INSTALL_PATHS if (work / name).exists()]
    existing = [
        name
        for name in INSTALL_PATHS
        if (state_dir / name).exists() or (state_dir / name).is_symlink()
    ]
    if existing and not replace:
        target = state_dir / existing[0]
        raise FileExistsError(f"{target} exists; pass --replace after stopping daemon")
    previous = state_dir / "migration-previous"
    if replace:
        # Keep a stopped copy for manual rollback, including any journal sidecars.
        previous.mkdir(mode=0o700)
    saved, installed = [], []
    try:
        for name in existing:
            os.replace(state_dir / name, previous / name)
            saved.append(name)
        for name in incoming:
            os.replace(work / name, state_dir / name)
            installed.append(name)
    except OSError:
        for name in reversed(installed):
            os.replace(state_dir / name, work / name)
        for name in reversed(saved):
            os.replace(previous / name, state_dir / name)
        if replace:
            previous.rmdir()
        raise


def import_state(archive_path, state_dir, *, replace=False):
    state_dir = Path(state_dir).resolve()
    with (
        inactive(state_dir),
        tempfile.TemporaryDirectory(prefix="state-import-", dir=state_dir) as temporary,
    ):
        work = Path(temporary)
        databases, remaining = stage_archive(archive_path, work)
        for name in databases:
            relative = Path(name)
            if relative.name == "bridge.sqlite":
                rewrite_snapshots(work / name, relative.parent, state_dir, remaining)
        if remaining:
            raise ValueError("unreferenced snapshot in archive")
        install_state(work, state_dir, replace=replace)


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
