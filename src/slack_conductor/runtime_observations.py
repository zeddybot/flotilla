"""Compatibility view for Slack's durable poller over the session-runtime API.

The network speaks observe/recover; this view preserves existing server state
and cursor semantics without exposing a client's database or native transcript.
"""


class RuntimeObservations:
    def __init__(self, remote):
        self.remote = remote

    def online(self):
        return self.remote.ready()

    def _observe(self, sid, **query):
        return self.remote.rpc("runtime.observe", {"session_id": sid, **query})

    def workspace(self, wid):
        sid = next((s for s, w in self.remote.bindings.items() if w == wid), None)
        if sid is None:
            raise ValueError("Workspace is not bound to this server")
        return self._observe(sid, view="workspace")["workspace"]

    def session(self, sid):
        from .conductor.journal import Unconfirmed

        try:
            return self._observe(sid, view="snapshot")["snapshot"]
        except Unconfirmed:
            return {"status": "reconnecting"}

    def cursor(self, sid):
        return self._observe(sid, view="cursor")["cursor"]

    def messages(self, sid, after):
        return self._observe(sid, after=after, view="events")["events"]

    def pickup(self, sid, prompt):
        receipt = self._observe(sid, prompt=prompt, view="receipt")["receipt"]
        if receipt["state"] == "picked_up":
            return {"seq": receipt["seq"], "turn_id": receipt["segment_id"]}
        return None

    def pending(self, sid, prompt):
        return self._observe(sid, prompt=prompt, view="receipt")["receipt"]["state"] == "queued"

    def has_later_turn(self, sid, after, turn_id):
        return self._observe(sid, after=after, segment_id=turn_id, view="later_input")[
            "later_input"
        ]

    def latest_turn(self, sid):
        return self._observe(sid, view="latest_segment")["latest_segment"]
