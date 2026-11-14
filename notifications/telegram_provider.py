"""Phase 10.5: Telegram Bot Provider (sendMessage + optional sendPhoto)."""

import json
import uuid
import urllib.request


def send(event, cfg):
    token = (cfg.get("bot_token") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False, "Telegram bot_token or chat_id not configured"

    timeout_s = float(cfg.get("timeout_s", 5.0))
    text = f"[{event.severity.upper()}] {event.title}\n{event.message}\nTime: {event.timestamp}"

    if cfg.get("send_photo", True) and event.image_bytes:
        boundary = f"----SentinelBoundary{uuid.uuid4().hex}"
        parts = []
        for k, v in (("chat_id", chat_id), ("caption", text)):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"alert.jpg\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n".encode("utf-8") + event.image_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST"
        )
    else:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return True, "Telegram sent"
        return False, f"Telegram API error: {data}"
