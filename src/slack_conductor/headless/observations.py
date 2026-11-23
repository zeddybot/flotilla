"""Compatibility observation views over the headless event and session journals."""

import time

from ..runtime import RUNTIME_VERSION

VIEWS = frozenset(
    {"all", "snapshot", "events", "workspace", "cursor", "receipt", "later_input", "latest_segment"}
)


def observe(runtime, session_id, after=0, prompt=None, segment_id=None, view="all", limit=200):
    row = runtime._session(session_id)
    if view not in VIEWS:
        raise ValueError("Unknown observation view")
    page = (
        runtime.events.page(session_id, after, limit=limit) if view in {"all", "events"} else None
    )
    with runtime.store.connect() as db:
        active = db.execute(
            "SELECT id FROM turns WHERE session_id=? AND state IN ('launching','running')",
            (session_id,),
        ).fetchall()
        receipt = (
            db.execute(
                "SELECT * FROM turns WHERE session_id=? AND prompt=? ORDER BY rowid LIMIT 1",
                (session_id, prompt),
            ).fetchone()
            if prompt is not None
            else None
        )
        latest = db.execute(
            "SELECT id,state,result_seq FROM turns WHERE session_id=? AND pickup_seq IS NOT NULL AND segment_id IS NULL "
            "ORDER BY pickup_seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        later = db.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND pickup_seq>? AND coalesce(segment_id,id)!=? LIMIT 1",
            (session_id, after, segment_id),
        ).fetchone()
        pending_jobs = [
            item["id"]
            for item in db.execute(
                "SELECT id FROM jobs WHERE session_id=? AND state='active' ORDER BY id",
                (session_id,),
            )
        ]
    attention = row["activity"] == "attention"
    fresh = runtime.ready()
    freshness = "fresh" if fresh else "stale"
    rest = {
        "confirmed": fresh
        and not attention
        and not active
        and not pending_jobs
        and (latest is None or latest["state"] in {"done", "failed"}),
        "result_candidate": bool(latest and latest["result_seq"] is not None),
        "pending_jobs": pending_jobs,
        "awaiting_callback": bool(pending_jobs),
    }
    snapshot = {
        "id": session_id,
        "status": "needs_user_input" if attention else "working" if active else "idle",
        "runtime": {
            "instance_id": runtime.instance_id,
            "activity": row["activity"],
            "jobs": [{"id": item["id"]} for item in active] + [{"id": job} for job in pending_jobs],
        },
        "freshness": freshness,
        "configuration": runtime.describe(session_id)["configuration"],
        "rest": rest,
    }
    if view in {"all", "snapshot"}:
        runtime.events.snapshot(session_id, snapshot)
    return {
        "version": RUNTIME_VERSION,
        "session_id": session_id,
        "observed_at": time.time(),
        "snapshot": snapshot if view in {"all", "snapshot"} else None,
        "events": page["events"] if page else [],
        "workspace": {
            "id": row["workspace_id"],
            "state": row["state"],
            "workspace_path": row["path"],
        }
        if view == "workspace"
        else None,
        "cursor": runtime.events.cursor(session_id)
        if view == "cursor"
        else page["cursor"]
        if page
        else after,
        "receipt": _receipt(receipt) if prompt is not None else None,
        "later_input": bool(later) if view == "later_input" else None,
        "latest_segment": latest["id"] if latest and view == "latest_segment" else None,
        "has_more": page["has_more"] if page else False,
        "retained_after": page["retained_after"] if page else 0,
        "freshness": freshness,
        "rest": rest,
    }


def _receipt(turn):
    if turn is None:
        return {"state": "received", "seq": None, "segment_id": None}
    if turn["pickup_seq"] is not None:
        return {
            "state": "picked_up",
            "seq": turn["pickup_seq"],
            "segment_id": turn["segment_id"] or turn["id"],
        }
    if turn["segment_id"] is not None:
        state = {"steering": "pending"}.get(turn["state"], turn["state"])
    else:
        state = "launch_unconfirmed" if turn["state"] == "attention" else "queued"
    return {"state": state, "seq": None, "segment_id": None}
