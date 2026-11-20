import json

import pytest

from slack_conductor.conductor.events import normalize
from slack_conductor.headless.engines import decode
from slack_conductor.lifecycle import Turn
from slack_conductor.presentation import live, pages


@pytest.mark.parametrize("adapter", ["conductor", "headless"])
def test_stream_replay_and_final_replacement(adapter):
    turn = Turn()
    stream = [
        {"type": "message_start", "message": {"id": "m"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "part"},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ial"}},
    ]
    for seq, raw in enumerate(stream, 1):
        source = json.dumps({"type": "stream_event", "event": raw})
        row = {"role": "assistant", "seq": seq, "content": source}
        event = normalize(row)["event"] if adapter == "conductor" else decode("claude", source)[1]
        turn.consume({**row, "event": event})
        turn = Turn.load(turn.dump())
    assert live(turn) == "⚙️ *Working*\n\npartial"
    completed = json.dumps({"type": "result", "result": "revised answer"})
    row = {"role": "assistant", "seq": 4, "content": completed}
    event = normalize(row)["event"] if adapter == "conductor" else decode("claude", completed)[1]
    turn.consume({**row, "event": event})
    assert pages("done", turn.content) == ["✅ *Done*\n\nrevised answer"]
