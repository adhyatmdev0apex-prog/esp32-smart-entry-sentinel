"""Phase 11 & 12 Automated Security Hardening & Industrial Readiness Tests."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from security_hardening import (
    SecurityGuard,
    is_safe_filename,
    is_safe_id,
    mask_notification_secrets,
    strip_masked_secrets,
)


def run_security_tests():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "security_config.json")
        guard = SecurityGuard(config_path=cfg_path)

        # 1. Scrypt PIN hashing + brute-force lockout after 5 failures
        guard.set_pin("8492")
        assert guard.has_pin() is True
        ok, _ = guard.check_pin("8492", "10.0.0.5")
        assert ok is True
        for _ in range(5):
            ok_bad, _ = guard.check_pin("0000", "10.0.0.9")
            assert ok_bad is False
        ok_locked, msg_locked = guard.check_pin("8492", "10.0.0.9")
        assert ok_locked is False and "Locked" in msg_locked
        print("[PASS] 1. Scrypt PIN + 5-attempt brute-force IP lockout")

        # 2. Path traversal & identifier sanitization
        assert is_safe_id("person_12") is True
        assert is_safe_id("../person") is False
        assert is_safe_id("person_1;rm -rf") is False
        assert is_safe_filename("20261114_face0.jpg") is True
        assert is_safe_filename("../../secret.json") is False
        assert is_safe_filename("dir/file.jpg") is False
        print("[PASS] 2. Path traversal & ID sanitization")

        # 3. Rate limiter (40 mutations per 10s window)
        ip = "10.0.0.77"
        allowed = [guard.check_rate_limit(ip, is_mutation=True) for _ in range(41)]
        assert all(allowed[:40]) and allowed[40] is False
        print("[PASS] 3. Per-IP mutation rate limiter")

        # 4. Secret masking & placeholder stripping
        sample_cfg = {
            "providers": {
                "telegram": {"enabled": True, "bot_token": "123456:ABCDEF9876", "chat_id": "999"},
                "webhook": {"enabled": True, "url": "https://example.com", "secret": "supersecretkey"},
            }
        }
        masked = mask_notification_secrets(sample_cfg)
        assert masked["providers"]["telegram"]["bot_token"] == "***9876"
        assert masked["providers"]["webhook"]["secret"] == "***tkey"
        stripped = strip_masked_secrets(masked)
        assert "bot_token" not in stripped["providers"]["telegram"]
        assert "secret" not in stripped["providers"]["webhook"]
        print("[PASS] 4. Notification secret masking & safe round-trip stripping")

        # 5. API key rotation
        old_key = guard.config["api_key"]
        new_key = guard.rotate_api_key()
        assert old_key != new_key and len(new_key) >= 32
        print("[PASS] 5. Cryptographic API key generation & rotation")

    print("\nALL PHASE 11 & 12 SECURITY TESTS PASSED!")


if __name__ == "__main__":
    run_security_tests()
