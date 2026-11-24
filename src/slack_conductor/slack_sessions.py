"""Render owned Slack thread bindings against each client's current session inventory."""

import html
import re

from slack_sdk.errors import SlackClientError


def session_rows(bridge, bindings):
    inventory = (
        bridge.conductor.session_inventory()
        if hasattr(bridge.conductor, "session_inventory")
        else None
    )
    live = (
        {}
        if inventory is None
        else {(item["session_id"], item["workspace_id"]): item for item in inventory}
    )
    for row in bindings:
        model = row["model"]
        status = row["status"]
        if inventory is None:
            status = f"offline · last known {status}"
        else:
            item = live.get((row["session_id"], row["workspace_id"]))
            if item is None or item["state"] == "archived":
                continue
            config = item.get("configuration") or {}
            model = (
                (config.get("effective") or {}).get("model")
                or config.get("model")
                or (config.get("requested") or {}).get("model")
                or model
            )
            activity = item.get("activity")
            if activity in {"needs_user_input", "needs_user_permission", "needs_plan_response"}:
                status = "needs attention"
            elif activity in {"working", "running"}:
                status = "running"
            elif activity and (
                activity != "idle"
                or status not in {"queued", "starting", "delivering", "failed", "attention"}
            ):
                status = activity.replace("_", " ")
        if not model and row["event_key"]:
            # Older headless bindings stored the original client default only in the create receipt.
            receipts = getattr(bridge.conductor, "receipts", None)
            receipt = receipts and receipts.saved("runtime:create:" + row["event_key"])
            if receipt:
                created = receipt[1].get("value") or {}
                if created.get("session_id") == row["session_id"]:
                    model = created.get("spec", {}).get("model")
        yield row | {"model": model or "profile default", "status": status}


def session_listing(router, user):
    entries = []
    workspace_url = None
    for client_id, profile, _ in router.user_profiles(user):
        bridge = router.to_bridge(client_id)
        bindings = router.store.list_sessions(router.team_id, user, client_id, bridge.store)
        if not bindings:
            continue
        for row in session_rows(bridge, bindings):
            channel, ts = row["channel"], row["thread_ts"]
            if workspace_url is None:
                link = row["permalink"]
                if not link:
                    try:
                        link = router.slack.call(
                            "chat_getPermalink", channel=channel, message_ts=ts
                        )["permalink"]
                    except (OSError, SlackClientError):
                        link = ""
                match = re.match(r"^(https://[\w.-]+\.slack(?:-gov)?\.com)/archives/", link)
                workspace_url = match[1] if match else ""
            if workspace_url:
                url = f"{workspace_url}/archives/{channel}/p{ts.replace('.', '')}"
            else:
                url = (
                    f"https://app.slack.com/client/{router.team_id}/{channel}/thread/{channel}-{ts}"
                )
            preview = " ".join(row["prompt"].split())
            if len(preview) > 240:
                preview = preview[:239].rstrip() + "…"
            preview = re.sub(r"([\\`*_\[\]])", r"\\\1", html.escape(preview, quote=False))
            model = html.escape(row["model"], quote=False)
            line = f"**{profile}** · `{model}` · {row['status']} · [Open thread]({url})"
            entries.append(
                (row["updated_at"], f"{line}\n{preview or 'Opening message unavailable.'}")
            )
    return (
        "\n\n".join(entry for _, entry in sorted(entries, reverse=True))
        or "You have no active Slack sessions."
    )
