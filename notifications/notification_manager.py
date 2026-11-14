"""
Phase 10.1, 10.2, 10.8, 10.9: Notification Manager
Common NotificationEvent model, async queue, deduplication, cooldowns,
retries, and strict provider failure isolation.
"""

import time
import uuid
import queue
import threading
from collections import deque
from datetime import datetime

from notifications.notification_config import NotificationConfigStore
from notifications import (
    webhook_provider,
    mqtt_provider,
    telegram_provider,
    push_provider,
    whatsapp_provider,
)

PROVIDER_MODULES = {
    "webhook": webhook_provider,
    "mqtt": mqtt_provider,
    "telegram": telegram_provider,
    "push": push_provider,
    "whatsapp": whatsapp_provider,
}


class NotificationEvent:
    def __init__(
        self,
        event_type,
        title,
        message,
        severity="info",
        person_id=None,
        person_label=None,
        image_bytes=None,
        metadata=None,
        dedup_key=None,
    ):
        self.event_id = uuid.uuid4().hex[:12]
        self.event_type = str(event_type).upper()
        self.title = str(title)
        self.message = str(message)
        self.severity = str(severity)
        self.person_id = person_id
        self.person_label = person_label
        self.image_bytes = image_bytes
        self.metadata = dict(metadata or {})
        self.timestamp = datetime.now().isoformat(timespec="seconds")
        self.created_epoch = time.time()
        self.dedup_key = dedup_key or f"{self.event_type}:{self.person_id or self.title}"

    def to_public_dict(self):
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "person_id": self.person_id,
            "person_label": self.person_label,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class NotificationManager:
    def __init__(self, config_path=None, start_worker=True):
        self.config_store = NotificationConfigStore(config_path) if config_path else NotificationConfigStore()
        self._queue = queue.Queue(maxsize=100)
        self._lock = threading.Lock()
        self._dedup_cache = {}          # dedup_key -> last_epoch
        self._cooldown_cache = {}       # (event_type, provider) -> last_epoch
        self.recent_alerts = deque(maxlen=50)
        self.stats = {
            name: {"sent": 0, "failed": 0, "skipped": 0, "last_error": None, "last_sent": None}
            for name in ("dashboard", "webhook", "mqtt", "telegram", "push", "whatsapp")
        }
        self._running = True
        if start_worker:
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def emit(self, event, bypass_dedup=False):
        """Enqueue a NotificationEvent asynchronously without blocking the caller."""
        cfg = self.config_store.get()
        if not cfg.get("enabled", True):
            return False, "notifications_disabled"

        now = time.time()
        dedup_window = float(cfg.get("dedup_window_s", 8.0))
        if not bypass_dedup:
            with self._lock:
                last = self._dedup_cache.get(event.dedup_key)
                if last is not None and (now - last) < dedup_window:
                    return False, "deduplicated"
                self._dedup_cache[event.dedup_key] = now

        try:
            self._queue.put_nowait(event)
            return True, event.event_id
        except queue.Full:
            # Drop oldest item so newest security alerts always get through
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
                return True, event.event_id
            except Exception:
                return False, "queue_full"

    def _worker_loop(self):
        while self._running:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.dispatch_sync(event)
            except Exception as e:
                print(f"[!] NotificationManager dispatch error: {e}")
            finally:
                self._queue.task_done()

    def dispatch_sync(self, event, force_providers=None, bypass_cooldown=False):
        """Route event to configured providers with per-provider cooldown, retry, and failure isolation."""
        cfg = self.config_store.get()
        if not cfg.get("enabled", True) and not force_providers:
            return {}

        rules = cfg.get("rules", {})
        targets = list(force_providers) if force_providers else list(rules.get(event.event_type, ["dashboard"]))
        cooldown_s = float(cfg.get("cooldown_s", 15.0))
        max_retries = int(cfg.get("max_retries", 2))
        backoff_s = float(cfg.get("retry_backoff_s", 1.5))

        results = {}
        now = time.time()

        for pname in targets:
            pcfg = cfg.get("providers", {}).get(pname, {})
            if not force_providers and not pcfg.get("enabled", False):
                results[pname] = {"ok": False, "skipped": True, "detail": "provider disabled"}
                continue

            if not bypass_cooldown and pname != "dashboard":
                with self._lock:
                    last_p = self._cooldown_cache.get((event.event_type, pname))
                    if last_p is not None and (now - last_p) < cooldown_s:
                        self.stats[pname]["skipped"] += 1
                        results[pname] = {"ok": False, "skipped": True, "detail": "cooldown"}
                        continue

            if pname == "dashboard":
                with self._lock:
                    self.recent_alerts.appendleft(event.to_public_dict())
                    self.stats["dashboard"]["sent"] += 1
                    self.stats["dashboard"]["last_sent"] = event.timestamp
                results["dashboard"] = {"ok": True, "detail": "Added to dashboard alert feed"}
                continue

            mod = PROVIDER_MODULES.get(pname)
            if mod is None:
                results[pname] = {"ok": False, "detail": f"Unknown provider: {pname}"}
                continue

            ok = False
            detail = ""
            attempts = 1 + max(0, max_retries)
            for attempt in range(attempts):
                try:
                    ok, detail = mod.send(event, pcfg)
                    if ok:
                        break
                except Exception as exc:
                    ok = False
                    detail = str(exc)
                if attempt < attempts - 1:
                    time.sleep(min(backoff_s * (attempt + 1), 3.0))

            with self._lock:
                if ok:
                    self._cooldown_cache[(event.event_type, pname)] = time.time()
                    self.stats[pname]["sent"] += 1
                    self.stats[pname]["last_error"] = None
                    self.stats[pname]["last_sent"] = event.timestamp
                else:
                    self.stats[pname]["failed"] += 1
                    self.stats[pname]["last_error"] = detail

            results[pname] = {"ok": ok, "detail": detail}

        return results

    def get_status(self):
        cfg = self.config_store.get()
        # Mask secrets in status response
        providers_summary = {}
        for pname, pcfg in cfg.get("providers", {}).items():
            providers_summary[pname] = {
                "enabled": bool(pcfg.get("enabled", False)),
                **self.stats.get(pname, {}),
            }
        with self._lock:
            alerts = list(self.recent_alerts)[:25]
            qsize = self._queue.qsize()
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "queue_size": qsize,
            "providers": providers_summary,
            "rules": cfg.get("rules", {}),
            "recent_alerts": alerts,
        }


notification_manager = NotificationManager()
