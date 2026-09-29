"""Explicit engine policy; unsupported inner sandboxes never trigger a fallback."""

import os
import sys

POLICIES = {"read-only", "workspace-write", "external-sandbox"}


def validate(policy):
    if policy not in POLICIES:
        raise ValueError("Unknown headless permission policy")
    if policy == "external-sandbox" and (
        sys.platform != "linux" or os.environ.get("FLOTILLA_ISOLATED_WORKER") != "1"
    ):
        raise ValueError(
            "external-sandbox requires an isolated Linux worker with FLOTILLA_ISOLATED_WORKER=1"
        )
    return policy


def within_policy(requested, configured):
    validate(requested)
    validate(configured)
    ranks = {"read-only": 0, "workspace-write": 1, "external-sandbox": 2}
    if ranks[requested] > ranks[configured]:
        raise ValueError("Requested permission policy exceeds this worker's configured policy")
