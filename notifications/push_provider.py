"""Phase 10.6: Device Push Provider (ntfy.sh, Pushover, or generic HTTP push)."""

import json
import urllib.request
import urllib.parse


def send(event, cfg):
    service = (cfg.get("service") or "ntfy").lower()
    timeout_s = float(cfg.get("timeout_s", 4.0))

    if service == "pushover":
        token = (cfg.get("token") or "").strip()
        user_key = (cfg.get("user_key") or "").strip()
        if not token or not user_key:
            return False, "Pushover token or user_key not configured"
        body = urllib.parse.urlencode({
            "token": token,
            "user": user_key,
            "title": event.title,
            "message": event.message,
            "priority": "1" if event.severity == "critical" else "0",
        }).encode("utf-8")
        req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=body, method="POST")
    else:
        url = (cfg.get("url") or "").strip()
        if not url:
            return False, "Push URL (e.g. https://ntfy.sh/topic) not configured"
        if service == "ntfy":
            headers = {
                "Title": event.title,
                "Priority": "high" if event.severity in ("warning", "critical") else "default",
                "Tags": event.event_type.lower(),
            }
            token = (cfg.get("token") or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, data=event.message.encode("utf-8"), headers=headers, method="POST")
        else:
            body = json.dumps({
                "title": event.title,
                "message": event.message,
                "event_type": event.event_type,
                "severity": event.severity,
                "timestamp": event.timestamp,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if 200 <= resp.status < 300:
            return True, f"Push HTTP {resp.status}"
        return False, f"Push HTTP {resp.status}"
