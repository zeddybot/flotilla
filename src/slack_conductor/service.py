import fcntl
import logging
import os
import signal
import threading
from logging.handlers import RotatingFileHandler

from .config import credentials
from .slack_app import run
from .store import Store


def daemon(root, config, conductor):
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
    try:
        run(config, Store(config.state_dir / "bridge.sqlite"), conductor, credentials(root), stop)
    except Exception:
        logging.getLogger(__name__).exception("Daemon stopped")
        raise
    finally:
        lock.close()
