"""Subprocess boundary: isolated cwd, process group, and bounded shutdown."""

import os
import signal
import subprocess


def launch(args, cwd):
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        bufsize=1,
    )


def terminate(process):
    if getattr(process, "_flotilla_terminated", False):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=2)
        process._flotilla_terminated = True
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while children still own the process group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for an empty, just-reaped group. Only ignore
        # that race when a process inventory proves no live member remains.
        rows = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True
        ).stdout.splitlines()
        if any(
            parts[0] == str(process.pid) and not parts[1].startswith("Z")
            for line in rows
            if len(parts := line.split()) == 2
        ):
            raise
    process.wait(timeout=2)
    process._flotilla_terminated = True
