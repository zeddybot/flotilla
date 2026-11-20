"""Subprocess boundary: isolated cwd, process group, and bounded shutdown."""

import json
import os
import selectors
import signal
import subprocess
import time


class Rejected(RuntimeError):
    """The native engine rejected the request before accepting input."""


def write_json_line(fd, message, deadline):
    data = memoryview((json.dumps(message) + "\n").encode())
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while data:
            try:
                data = data[os.write(fd, data) :]
            except BlockingIOError:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("Headless stdin did not accept input")


def launch(args, cwd, *, duplex=False):
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE if duplex else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        bufsize=1,
    )


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS may report EPERM for a group whose only remaining process is
        # a zombie. Verify that no live member needs this signal.
        rows = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True
        ).stdout.splitlines()
        if any(
            parts[0] == str(process.pid) and not parts[1].startswith("Z")
            for line in rows
            if len(parts := line.split()) == 2
        ):
            raise
        return False
    return True


def terminate(process):
    if getattr(process, "_flotilla_terminated", False):
        return
    if _signal_group(process, signal.SIGTERM):
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # Children may still own the process group even after the leader exits.
        _signal_group(process, signal.SIGKILL)
    process.wait(timeout=2)
    process._flotilla_terminated = True
