"""Shared bridge test doubles and native-message builders.

Keep test scenarios in their own modules; this module supplies only the common
in-memory Slack recorder and message builders used by those scenarios.
"""

import json


class Slack:
    def __init__(self):
        self.posts, self.edits, self.deletes = [], [], []

    def post(self, channel, thread_ts, text, key, *, status_header=True):
        self.posts.append((channel, thread_ts, text, key))
        return str(len(self.posts))

    def edit(self, status, text):
        self.edits.append((status["status_ts"], text))

    def delete(self, status):
        self.deletes.append(status["status_ts"])


def status(store, key, prompt, cursor=0, *, send_mode="steer"):
    sid = store.add_status(key, "C1", "1", prompt, {"verbose": False, "send_mode": send_mode})
    store.update(
        sid,
        status_ts=f"status-{sid}",
        workspace_id="w",
        session_id="s",
        state="waiting",
        cursor=cursor,
    )
    return sid


def message(conn, role, text, turn, sent="now"):
    conn.execute(
        "INSERT INTO session_messages VALUES (hex(randomblob(8)),'s',?,?,?,?,NULL,'same-time')",
        (role, text, turn, sent),
    )
    conn.commit()


def result(conn, text, turn):
    message(conn, "assistant", json.dumps({"type": "result", "result": text}), turn)


def event(conn, subtype, *, turn="turn", **values):
    message(conn, "assistant", json.dumps({"type": "system", "subtype": subtype, **values}), turn)
