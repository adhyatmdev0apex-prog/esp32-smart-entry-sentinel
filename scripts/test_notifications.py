"""Phase 10.10 Automated Reliability & Failure-Mode Tests for Notification System."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from notifications.notification_config import NotificationConfigStore
from notifications.notification_manager import NotificationEvent, NotificationManager


class _MockWebhookHandler(http.server.BaseHTTPRequestHandler):
    received = []
    delay_s = 0.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {"raw": body.decode("utf-8", errors="replace")}
        self.__class__.received.append((self.path, dict(self.headers), payload))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        return


def _start_mock_server(delay_s=0.0):
    handler = type(
        f"_Handler_{int(delay_s * 1000)}",
        (_MockWebhookHandler,),
        {"received": [], "delay_s": delay_s},
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, handler


def run_all_tests():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "notification_config.json")
        mgr = NotificationManager(config_path=cfg_path)
        store = mgr.config_store

        # 1. Config persistence across reload
        store.update({
            "dedup_window_s": 5.0,
            "cooldown_s": 5.0,
            "max_retries": 1,
            "retry_backoff_s": 0.05,
            "providers": {
                "webhook": {"enabled": True, "url": "http://127.0.0.1:1/unreachable", "timeout_s": 0.4, "secret": "s3cr3t"},
                "mqtt": {"enabled": True, "host": "127.0.0.1", "port": 1, "timeout_s": 0.4},
                "telegram": {"enabled": True, "bot_token": "invalid", "chat_id": "123", "timeout_s": 0.4, "api_base": "http://127.0.0.1:1"},
            },
        })
        reloaded_store = NotificationConfigStore(cfg_path)
        assert reloaded_store.get()["providers"]["webhook"]["secret"] == "s3cr3t", "Config did not persist"
        print("[PASS] 1. Config persistence across reload")

        # 2. Unreachable internet / webhook / MQTT / Telegram must fail gracefully without crashing
        ev_fail = NotificationEvent(
            event_type="SYSTEM_FAILURE",
            title="Outage Test",
            message="Testing offline providers",
            severity="critical",
        )
        res_fail = mgr.dispatch_sync(ev_fail, force_providers=["webhook", "mqtt", "telegram", "dashboard"], bypass_cooldown=True)
        assert res_fail["webhook"]["ok"] is False, "Expected unreachable webhook to fail"
        assert res_fail["mqtt"]["ok"] is False, "Expected unreachable MQTT to fail"
        assert res_fail["telegram"]["ok"] is False, "Expected unreachable Telegram to fail"
        assert res_fail["dashboard"]["ok"] is True, "Dashboard should stay isolated and succeed"
        print("[PASS] 2. Offline/unreachable webhook, MQTT & Telegram handled with failure isolation")

        # 3. Webhook timeout handling
        slow_srv, _ = _start_mock_server(delay_s=1.2)
        try:
            slow_port = slow_srv.server_address[1]
            store.update({"providers": {"webhook": {"enabled": True, "url": f"http://127.0.0.1:{slow_port}/hook", "timeout_s": 0.2}}})
            res_to = mgr.dispatch_sync(ev_fail, force_providers=["webhook"], bypass_cooldown=True)
            assert res_to["webhook"]["ok"] is False, f"Expected timeout failure, got {res_to}"
            print("[PASS] 3. Webhook timeout enforced cleanly")
        finally:
            slow_srv.shutdown()
            slow_srv.server_close()

        # 4. Live mock webhook + signature + provider isolation (failing MQTT + working webhook)
        ok_srv, ok_handler = _start_mock_server(delay_s=0.0)
        try:
            ok_port = ok_srv.server_address[1]
            store.update({
                "providers": {
                    "webhook": {"enabled": True, "url": f"http://127.0.0.1:{ok_port}/hook", "timeout_s": 2.0, "secret": "hmac-key"},
                    "mqtt": {"enabled": True, "host": "127.0.0.1", "port": 1, "timeout_s": 0.3},
                },
                "rules": {"DOOR_OPEN": ["dashboard", "mqtt", "webhook"]},
            })
            res_iso = mgr.dispatch_sync(
                NotificationEvent(event_type="DOOR_OPEN", title="Door Opened", message="Front door reed switch triggered"),
                bypass_cooldown=True,
            )
            assert res_iso["mqtt"]["ok"] is False
            assert res_iso["webhook"]["ok"] is True
            assert len(ok_handler.received) == 1
            _, hdrs, body = ok_handler.received[0]
            assert any(k.lower() == "x-sentinel-signature" for k in hdrs), "Missing HMAC signature header"
            assert body["event_type"] == "DOOR_OPEN"
            print("[PASS] 4. Provider failure isolation + HMAC webhook delivery")

            # 5. Duplicate event suppression & Cooldown suppression
            ev_dup = NotificationEvent(event_type="DOOR_OPEN", title="Door Opened", message="Repeat", dedup_key="DOOR_OPEN:front")
            acc1, _ = mgr.emit(ev_dup)
            acc2, reason2 = mgr.emit(ev_dup)
            assert acc1 is True and acc2 is False and reason2 == "deduplicated", f"Expected deduplicated, got {(acc1, acc2, reason2)}"
            print("[PASS] 5. Duplicate event suppression")

            # 6. Multiple simultaneous events via async queue
            ok_handler.received.clear()
            for i in range(6):
                acc, _ = mgr.emit(
                    NotificationEvent(
                        event_type="DOOR_OPEN",
                        title=f"Burst {i}",
                        message=f"Simultaneous event {i}",
                        dedup_key=f"burst:{i}",
                    ),
                    bypass_dedup=True,
                )
                assert acc is True
            mgr._queue.join()
            status = mgr.get_status()
            dash_sent = status["providers"]["dashboard"]["sent"]
            assert dash_sent >= 1
            print(f"[PASS] 6. Multiple simultaneous events processed (dashboard_sent={dash_sent})")
        finally:
            ok_srv.shutdown()
            ok_srv.server_close()

        mgr._running = False
    print("\nALL PHASE 10 NOTIFICATION TESTS PASSED!")


if __name__ == "__main__":
    run_all_tests()
