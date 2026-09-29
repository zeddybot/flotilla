import fcntl
import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler

from .config import credentials
from .slack_app import run
from .store import Store


def enforce_shutdown(stop, *, timeout=30, exit_process=os._exit):
    """Give cleanup a grace period, then release launchd even if SDK threads hang."""
    stop.wait()
    time.sleep(timeout)
    # Do not use sys.exit: it exits only this thread. Keep this guard alive until
    # process exit, including interpreter shutdown waiting for non-daemon workers.
    exit_process(1)


def server(root, config, conductor):
    lock, stop = _start(config)
    try:
        run(config, Store(config.state_dir / "bridge.sqlite"), conductor, credentials(root), stop)
    except Exception:
        logging.getLogger(__name__).exception("Server stopped")
        raise
    finally:
        stop.set()
        lock.close()


def client(root, config, token):
    """Run one Mac executor for this state directory."""
    from .remote import run_client

    lock, stop = _start(config)
    try:
        run_client(config, token, stop)
    except Exception:
        logging.getLogger(__name__).exception("Client stopped")
        raise
    finally:
        stop.set()
        lock.close()


def _start(config):
    os.umask(0o077)
    lock = (config.state_dir / "daemon.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another Conductor Slack daemon is already running") from None
    handler = RotatingFileHandler(
        config.state_dir / "daemon.log", maxBytes=2_000_000, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(
        target=enforce_shutdown, args=(stop,), name="shutdown-deadline", daemon=True
    ).start()
    return lock, stop
