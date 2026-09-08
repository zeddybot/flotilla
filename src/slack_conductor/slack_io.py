import time
import uuid

from slack_sdk.errors import SlackApiError


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
        return self.call(
            "chat_postMessage",
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            unfurl_links=False,
            unfurl_media=False,
        )["ts"]

    def edit(self, status, text):
        self.call("chat_update", channel=status["channel"], ts=status["status_ts"], text=text)

    def delete(self, status):
        try:
            self.call("chat_delete", channel=status["channel"], ts=status["status_ts"])
        except SlackApiError as exc:
            if exc.response.get("error") != "message_not_found":
                raise
