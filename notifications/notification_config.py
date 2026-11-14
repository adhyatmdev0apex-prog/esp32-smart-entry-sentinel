"""
Phase 10.1 & 10.8: Notification Configuration & Routing Rules
Persists provider settings and event->provider rules in data/notification_config.json.
"""

import os
import json
import copy
import threading

CONFIG_PATH = os.path.join("data", "notification_config.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "cooldown_s": 15.0,
    "dedup_window_s": 8.0,
    "max_retries": 2,
    "retry_backoff_s": 1.5,
    "rules": {
        "DOOR_OPEN": ["dashboard", "mqtt", "push"],
        "MOTION_DETECTED": ["dashboard", "mqtt"],
        "UNKNOWN_PERSON": ["dashboard", "push", "telegram", "webhook"],
        "KNOWN_PERSON": ["dashboard"],
        "OBJECT_DETECTED": ["dashboard"],
        "CAMERA_OFFLINE": ["dashboard", "push", "webhook"],
        "SYSTEM_FAILURE": ["dashboard", "telegram", "webhook"],
    },
    "providers": {
        "dashboard": {
            "enabled": True,
        },
        "webhook": {
            "enabled": False,
            "url": "",
            "secret": "",
            "timeout_s": 4.0,
        },
        "mqtt": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 1883,
            "username": "",
            "password": "",
            "base_topic": "sentinel",
            "timeout_s": 3.0,
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "send_photo": True,
            "timeout_s": 5.0,
        },
        "push": {
            "enabled": False,
            "service": "ntfy",  # ntfy | pushover | generic
            "url": "",
            "token": "",
            "user_key": "",
            "timeout_s": 4.0,
        },
        "whatsapp": {
            "enabled": False,
            "api_url": "",
            "api_key": "",
            "phone": "",
            "timeout_s": 5.0,
        },
    },
}


class NotificationConfigStore:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def _deep_merge(self, base, override):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    merged = copy.deepcopy(DEFAULT_CONFIG)
                    self._deep_merge(merged, loaded)
                    self.config = merged
                except Exception as e:
                    print(f"[!] NotificationConfig load error: {e}")
            else:
                self.save_unlocked()

    def save_unlocked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
        os.replace(tmp, self.path)

    def get(self):
        with self._lock:
            return copy.deepcopy(self.config)

    def update(self, patch):
        with self._lock:
            self._deep_merge(self.config, patch)
            self.save_unlocked()
            return copy.deepcopy(self.config)
