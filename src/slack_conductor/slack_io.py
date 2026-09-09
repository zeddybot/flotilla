import logging
import time
import uuid

from slack_sdk.errors import SlackApiError

from .presentation import blocks

log = logging.getLogger(__name__)


class SlackIO:
    def __init__(self, client):
        self.client = client

    def call(self, method, **kwargs):
        for attempt in range(5):
            try:
                return getattr(self.client, method)(**kwargs)
            except SlackApiError as exc:
                if exc.response.status_code != 429 or attempt == 4:
                    raise
                time.sleep(max(1, int(exc.response.headers.get("Retry-After", "1"))))

    def post(self, channel, thread_ts, text, key):
        return self._message(
            "chat_postMessage",
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            blocks=blocks(text),
            client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            unfurl_links=False,
            unfurl_media=False,
        )["ts"]

    def edit(self, status, text):
        self._message(
            "chat_update",
            channel=status["channel"],
            ts=status["status_ts"],
            text=text,
            blocks=blocks(text),
        )

    def _message(self, method, **kwargs):
        try:
            return self.call(method, **kwargs)
        except SlackApiError as exc:
            if exc.response.get("error") != "invalid_blocks":
                raise
            # Markdown expands server-side and can exceed Slack's 50-block limit.
            # The rejected write had no effect; deliver all text and clear stale blocks.
            log.warning("Slack rejected Markdown blocks; delivering text fallback")
            return self.call(method, **(kwargs | {"blocks": []}))

    def delete(self, status):
        try:
            self.call("chat_delete", channel=status["channel"], ts=status["status_ts"])
        except SlackApiError as exc:
            if exc.response.get("error") != "message_not_found":
                raise
