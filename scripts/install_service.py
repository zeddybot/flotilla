"""Install the Mac client runtime and per-user LaunchAgent."""

import argparse
from pathlib import Path

from slack_conductor.cli_config import discover_root
from slack_conductor.service_manager import (
    LABEL,
    client_install,
    client_uninstall,
    plist,
    runtime_config,
    unload,
)

__all__ = ["LABEL", "client_install", "client_uninstall", "plist", "runtime_config", "unload"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="directory containing config.toml")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.uninstall and args.prepare_only:
        parser.error("--uninstall and --prepare-only cannot be combined")
    if args.uninstall:
        print(client_uninstall())
    else:
        print(client_install(discover_root(args.root), prepare_only=args.prepare_only))


if __name__ == "__main__":
    main()
