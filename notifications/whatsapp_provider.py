"""Phase 10.7: Optional WhatsApp Gateway Provider (external API/webhook only)."""

import json
import urllib.request


def send(event, cfg):
    api_url = (cfg.get("api_url") or "").strip()
    phone = (cfg.get("phone") or "").strip()
    if not api_url or not phone:
        return False, "WhatsApp api_url or phone not configured"

    timeout_s = float(cfg.get("timeout_s", 5.0))
    headers = {"Content-Type": "application/json"}
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = json.dumps({
        "to": phone,
        "text": f"*{event.title}*\n{event.message}\n({event.timestamp})",
        "event_type": event.event_type,
        "severity": event.severity,
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if 200 <= resp.status < 300:
            return True, f"WhatsApp HTTP {resp.status}"
        return False, f"WhatsApp HTTP {resp.status}"
