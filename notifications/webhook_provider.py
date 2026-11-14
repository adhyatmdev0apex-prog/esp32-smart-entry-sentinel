"""Phase 10.3: Universal Webhook Provider (HTTP POST JSON + optional HMAC signature)."""

import hmac
import hashlib
import json
import urllib.request
import urllib.error


def send(event, cfg):
    url = (cfg.get("url") or "").strip()
    if not url:
        return False, "Webhook URL not configured"

    timeout_s = float(cfg.get("timeout_s", 4.0))
    payload = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "severity": event.severity,
        "title": event.title,
        "message": event.message,
        "content": f"**{event.title}** — {event.message}",  # Discord/Slack friendly
        "person_id": event.person_id,
        "person_label": event.person_label,
        "timestamp": event.timestamp,
        "metadata": event.metadata,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ESP32-Sentinel/1.0",
    }
    secret = (cfg.get("secret") or "").strip()
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Sentinel-Signature"] = f"sha256={sig}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if 200 <= resp.status < 300:
            return True, f"HTTP {resp.status}"
        return False, f"HTTP {resp.status}"
