"""
Phase 11 & 12: Industrial-Grade Security Hardening Module
---------------------------------------------------------
Provides defense-in-depth for the Sentinel Smart Door Entry Server:
- Scrypt-hashed Admin PIN & cryptographically random API Key / HMAC secret (data/security_config.json)
- Remote/LAN mutation protection (loopback trusted unless 'lock_localhost' is enabled)
- Sliding-window per-IP rate limiting + brute-force lockout
- Strict path-traversal & identifier sanitization
- Security HTTP headers (X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, no-store on /api/*)
- Secret masking for notification provider credentials
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, request, session

SECURITY_CONFIG_PATH = os.path.join("data", "security_config.json")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

MUTATION_PREFIXES = (
    "/api/person/",
    "/api/merge",
    "/api/system/",
    "/api/camera/",
    "/api/v1/notifications/config",
    "/api/v1/notifications/test",
    "/api/v1/security/",
    "/reset",
)


def _hash_pin(pin: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex(), dk.hex()


def _verify_pin(pin: str, salt_hex: str, expected_hex: str) -> bool:
    if not salt_hex or not expected_hex:
        return False
    _, actual_hex = _hash_pin(pin, salt_hex)
    return hmac.compare_digest(actual_hex, expected_hex)


class SecurityGuard:
    def __init__(self, config_path: str = SECURITY_CONFIG_PATH):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._rate_buckets: dict[str, deque[float]] = defaultdict(deque)
        self._failed_auth: dict[str, list] = {}  # ip -> [count, unlock_epoch]
        self.blocked_count = 0
        self.config = self._load_or_init()

    def _load_or_init(self) -> dict:
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("api_key") and data.get("flask_secret"):
                    return data
            except Exception:
                pass
        cfg = {
            "flask_secret": secrets.token_hex(32),
            "api_key": secrets.token_urlsafe(32),
            "hmac_secret": secrets.token_hex(32),
            "lock_localhost": False,
            "pin_salt": "",
            "pin_hash": "",
            "trusted_lan_events": True,  # Allow ESP32-WROOM / ESP32-CAM on LAN to POST /api/v1/event
        }
        self._save(cfg)
        return cfg

    def _save(self, cfg: dict | None = None):
        target = cfg if cfg is not None else self.config
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(target, f, indent=2)
        os.replace(tmp, self.config_path)

    def is_loopback(self, ip: str | None) -> bool:
        return ip in ("127.0.0.1", "::1", "localhost")

    def has_pin(self) -> bool:
        return bool(self.config.get("pin_hash"))

    def set_pin(self, pin: str):
        with self._lock:
            if not pin:
                self.config["pin_salt"] = ""
                self.config["pin_hash"] = ""
                self.config["lock_localhost"] = False
            else:
                salt_h, dk_h = _hash_pin(pin)
                self.config["pin_salt"] = salt_h
                self.config["pin_hash"] = dk_h
            self._save()

    def set_lock_localhost(self, locked: bool):
        with self._lock:
            if locked and not self.has_pin():
                raise ValueError("Set an Admin PIN before enabling localhost lock")
            self.config["lock_localhost"] = bool(locked)
            self._save()

    def rotate_api_key(self) -> str:
        with self._lock:
            self.config["api_key"] = secrets.token_urlsafe(32)
            self._save()
            return self.config["api_key"]

    def check_pin(self, pin: str, client_ip: str) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            rec = self._failed_auth.get(client_ip)
            if rec and now < rec[1]:
                rem = int(rec[1] - now)
                return False, f"Too many failed attempts. Locked for {rem}s."
        if not self.has_pin():
            return True, "no_pin_configured"
        ok = _verify_pin(pin, self.config.get("pin_salt", ""), self.config.get("pin_hash", ""))
        with self._lock:
            if ok:
                self._failed_auth.pop(client_ip, None)
                return True, "ok"
            rec = self._failed_auth.get(client_ip, [0, 0.0])
            rec[0] += 1
            if rec[0] >= 5:
                rec[1] = now + 120.0
                self.blocked_count += 1
            self._failed_auth[client_ip] = rec
            return False, "Invalid PIN"

    def check_rate_limit(self, client_ip: str, is_mutation: bool) -> bool:
        now = time.time()
        window = 10.0
        limit = 40 if is_mutation else 160
        key = f"{client_ip}:{'m' if is_mutation else 'r'}"
        with self._lock:
            q = self._rate_buckets[key]
            while q and (now - q[0]) > window:
                q.popleft()
            if len(q) >= limit:
                self.blocked_count += 1
                return False
            q.append(now)
            return True

    def is_authorized(self, client_ip: str, path: str, method: str) -> tuple[bool, str]:
        # Public read / auth / health endpoints
        if path in ("/api/v1/security/login", "/api/v1/security/status", "/api/v1/system/healthz"):
            return True, "public"

        # Allow ESP32-WROOM / ESP32-CAM on LAN to post hardware events
        if path == "/api/v1/event" and method == "POST" and self.config.get("trusted_lan_events", True):
            return True, "lan_event"

        # Check API Key header
        api_hdr = request.headers.get("X-Sentinel-API-Key", "")
        if api_hdr and hmac.compare_digest(api_hdr, self.config.get("api_key", "")):
            return True, "api_key"

        # Loopback trust unless lock_localhost is enabled
        if self.is_loopback(client_ip) and not self.config.get("lock_localhost", False):
            return True, "loopback"

        # Authenticated session cookie
        if session.get("sentinel_auth") is True:
            return True, "session"

        # Allow read-only GET dashboard assets when no PIN is configured on loopback;
        # require auth for all remote mutations and when locked
        is_mut = method in ("POST", "PUT", "DELETE", "PATCH") or any(path.startswith(p) for p in MUTATION_PREFIXES)
        if not is_mut and self.is_loopback(client_ip) and not self.config.get("lock_localhost", False):
            return True, "loopback_read"

        if not self.has_pin() and self.is_loopback(client_ip):
            return True, "loopback_no_pin"

        return False, "unauthorized"

    def get_public_status(self) -> dict:
        key = self.config.get("api_key", "")
        masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "configured"
        return {
            "pin_configured": self.has_pin(),
            "lock_localhost": bool(self.config.get("lock_localhost", False)),
            "api_key_preview": masked_key,
            "blocked_requests": self.blocked_count,
            "rate_limiter": "active (40 mut / 160 read per 10s)",
            "headers_hardened": True,
            "path_guard": True,
            "secret_masking": True,
        }


def is_safe_id(val: str | None) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def is_safe_filename(val: str | None) -> bool:
    if not val or ".." in val or "/" in val or "\\" in val or "\x00" in val:
        return False
    return bool(SAFE_FILENAME_RE.match(val))


def mask_notification_secrets(cfg: dict) -> dict:
    """Return a deep-ish copy of notification config with sensitive tokens masked."""
    out = json.loads(json.dumps(cfg))
    secret_fields = ("bot_token", "secret", "app_token", "user_key", "api_key", "password")
    for pcfg in out.get("providers", {}).values():
        if not isinstance(pcfg, dict):
            continue
        for k in secret_fields:
            v = pcfg.get(k)
            if isinstance(v, str) and len(v) > 4:
                pcfg[k] = "***" + v[-4:]
    return out


def strip_masked_secrets(patch: dict) -> dict:
    """Prevent UI saves with '***xxxx' masked placeholders from overwriting real secrets."""
    cleaned = json.loads(json.dumps(patch))
    secret_fields = ("bot_token", "secret", "app_token", "user_key", "api_key", "password")
    for pcfg in cleaned.get("providers", {}).values():
        if not isinstance(pcfg, dict):
            continue
        for k in secret_fields:
            v = pcfg.get(k)
            if isinstance(v, str) and v.startswith("***"):
                pcfg.pop(k, None)
    return cleaned


security_guard = SecurityGuard()


def init_app_security(app, audit_cb=None):
    """Attach Flask security secret, 16 MB upload cap, rate limiter, auth guard, and headers."""
    app.secret_key = security_guard.config["flask_secret"]
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.before_request
    def _security_before_request():
        client_ip = request.remote_addr or "unknown"
        path = request.path or "/"
        method = request.method or "GET"

        # Path traversal quick reject
        if ".." in path or "\x00" in path:
            security_guard.blocked_count += 1
            if audit_cb:
                audit_cb("security", "path_traversal_blocked", client_ip, path)
            return jsonify({"ok": False, "error": "Invalid path"}), 400

        # Rate limiting
        is_mut = method in ("POST", "PUT", "DELETE", "PATCH")
        if not security_guard.check_rate_limit(client_ip, is_mut):
            if audit_cb:
                audit_cb("security", "rate_limit_blocked", client_ip, f"{method} {path}")
            return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429

        # Authorization check for mutations / locked mode
        ok, reason = security_guard.is_authorized(client_ip, path, method)
        if not ok:
            security_guard.blocked_count += 1
            if audit_cb:
                audit_cb("security", "unauthorized_blocked", client_ip, f"{method} {path} ({reason})")
            return jsonify({"ok": False, "error": "Authentication required", "code": "AUTH_REQUIRED"}), 401

    @app.after_request
    def _security_after_request(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if (request.path or "").startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
