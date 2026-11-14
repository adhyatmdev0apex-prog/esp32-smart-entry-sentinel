"""
Smart Entry Sentinel — Control Center Server (:5001)
====================================================
D2/D8/D9: Dedicated Admin-only Control Center for:
- GitHub release checking, downloading, installing, and rollback
- Controlled whitelisted firmware/config editor (no terminal / no VS Code Web)
- PlatformIO build / clean / audit + artifact management
- OTA (via authoritative scripts/ota_upload.py) and USB Serial flashing
"""

import os
import urllib.request
from flask import Flask, jsonify, redirect, render_template, request, session

from cc_managers import (
    BuildManager,
    ReleaseManager,
    UploadManager,
    WorkspaceManager,
    load_cc_config,
    save_cc_config,
)
from event_db import event_db
from security_hardening import security_guard, verify_secrets_h_login

app = Flask(__name__)
app.secret_key = security_guard.config["flask_secret"]
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

workspace_mgr = WorkspaceManager()
release_mgr = ReleaseManager(workspace_mgr)
build_mgr = BuildManager()
upload_mgr = UploadManager()


@app.before_request
def _cc_security_guard():
    client_ip = request.remote_addr or "unknown"
    path = request.path or "/"
    method = request.method or "GET"

    if ".." in path or "\x00" in path:
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    is_mut = method in ("POST", "PUT", "DELETE", "PATCH")
    if not security_guard.check_rate_limit(f"cc:{client_ip}", is_mut):
        return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429

    if path in ("/login", "/logout", "/api/cc/healthz", "/favicon.ico"):
        return None

    # Allow loopback only when lock_localhost is false; remote LAN always requires ADmin login
    if security_guard.is_loopback(client_ip) and not security_guard.config.get("lock_localhost", False):
        return None

    if session.get("sentinel_auth") is True and session.get("auth_role") == "admin":
        return None

    if session.get("sentinel_auth") is True and session.get("auth_role") != "admin":
        if method == "GET" and not path.startswith("/api/"):
            return render_template("login.html", error="Control Center requires ADmin login."), 403
        return jsonify({"ok": False, "error": "ADmin login required for Control Center", "code": "ADMIN_REQUIRED"}), 403

    if method == "GET" and not path.startswith("/api/"):
        return redirect("/login")
    return jsonify({"ok": False, "error": "Authentication required", "code": "AUTH_REQUIRED"}), 401


@app.after_request
def _cc_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if (request.path or "").startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def cc_login():
    if request.method == "GET":
        if session.get("sentinel_auth") is True and session.get("auth_role") == "admin":
            return redirect("/")
        return render_template("login.html", error=None)

    data = request.get_json(silent=True)
    if isinstance(data, dict):
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
    else:
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")

    client_ip = request.remote_addr or "unknown"
    ok, role = verify_secrets_h_login(username, password)
    if ok and role == "admin":
        session["sentinel_auth"] = True
        session["auth_role"] = "admin"
        session["auth_user"] = username
        event_db.log_audit(username, "cc_login_success", client_ip, "role=admin")
        if isinstance(data, dict):
            return jsonify({"ok": True, "role": "admin"}), 200
        return redirect("/")

    msg = "Control Center requires ADmin credentials" if ok else "Invalid username or password"
    event_db.log_audit(username or "unknown", "cc_login_denied", client_ip, msg)
    if isinstance(data, dict):
        return jsonify({"ok": False, "error": msg}), 403 if ok else 401
    return render_template("login.html", error=msg), 403 if ok else 401


@app.route("/logout", methods=["GET", "POST"])
def cc_logout():
    session.pop("auth_user", None)
    session.pop("auth_role", None)
    session.pop("sentinel_auth", None)
    return redirect("/login")


@app.route("/", methods=["GET"])
def cc_index():
    return render_template("control_center.html")


@app.route("/api/cc/healthz", methods=["GET"])
def cc_healthz():
    sentinel_ok = False
    try:
        port_5000 = int(os.environ.get("PORT", 5000))
        with urllib.request.urlopen(f"http://127.0.0.1:{port_5000}/api/v1/system/healthz", timeout=1.5) as r:
            sentinel_ok = (r.status == 200)
    except Exception:
        sentinel_ok = False
    return jsonify({
        "ok": True,
        "service": "control_center",
        "sentinel_online": sentinel_ok,
        "build_status": build_mgr.status,
        "upload_status": upload_mgr.status,
    })


@app.route("/api/cc/config", methods=["GET", "POST"])
def cc_config_api():
    if request.method == "GET":
        return jsonify({"ok": True, "config": load_cc_config()})
    data = request.get_json(silent=True) or {}
    cfg = save_cc_config(data)
    event_db.log_audit("admin", "cc_config_updated", "cc_config.json", str(list(data.keys())))
    return jsonify({"ok": True, "config": cfg})


# --- D4: GitHub Release Manager ---
@app.route("/api/cc/releases/check", methods=["GET"])
def cc_releases_check():
    repo = request.args.get("repo")
    try:
        return jsonify(release_mgr.check_latest(repo))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/cc/releases/backups", methods=["GET"])
def cc_releases_backups():
    return jsonify({"ok": True, "backups": release_mgr.list_backups()})


@app.route("/api/cc/releases/install", methods=["POST"])
def cc_releases_install():
    data = request.get_json(silent=True) or {}
    try:
        res = release_mgr.install_latest(data.get("repo"))
        event_db.log_audit("admin", "cc_release_install", res.get("installed_version", ""), res.get("sha256", "")[:16])
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/cc/releases/rollback", methods=["POST"])
def cc_releases_rollback():
    data = request.get_json(silent=True) or {}
    try:
        res = release_mgr.rollback(data.get("backup"))
        event_db.log_audit("admin", "cc_release_rollback", res.get("restored", ""), "")
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# --- D5: Controlled Simple Editor ---
@app.route("/api/cc/editor/tree", methods=["GET"])
def cc_editor_tree():
    return jsonify({"ok": True, "files": workspace_mgr.list_tree()})


@app.route("/api/cc/editor/file", methods=["GET", "POST"])
def cc_editor_file():
    if request.method == "GET":
        rel = request.args.get("path", "")
        try:
            return jsonify({"ok": True, **workspace_mgr.read_file(rel)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    data = request.get_json(silent=True) or {}
    rel = str(data.get("path") or "")
    content = data.get("content")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "Missing content string"}), 400
    try:
        res = workspace_mgr.save_file(rel, content)
        event_db.log_audit("admin", "cc_editor_save", rel, f"bytes={len(content)}")
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/cc/editor/revert", methods=["POST"])
def cc_editor_revert():
    data = request.get_json(silent=True) or {}
    rel = str(data.get("path") or "")
    try:
        res = workspace_mgr.revert_file(rel)
        event_db.log_audit("admin", "cc_editor_revert", rel, "")
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# --- D6: Build Manager ---
@app.route("/api/cc/build/status", methods=["GET"])
def cc_build_status():
    return jsonify({"ok": True, **build_mgr.get_state()})


@app.route("/api/cc/build/audit", methods=["GET"])
def cc_build_audit():
    return jsonify(build_mgr.run_audit())


@app.route("/api/cc/build/start", methods=["POST"])
def cc_build_start():
    data = request.get_json(silent=True) or {}
    env = str(data.get("env") or "esp32cam")
    clean = bool(data.get("clean", False))
    try:
        res = build_mgr.start_build(env, clean=clean)
        event_db.log_audit("admin", "cc_build_start", env, f"clean={clean}")
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# --- D7: Upload Manager ---
@app.route("/api/cc/upload/status", methods=["GET"])
def cc_upload_status():
    return jsonify({"ok": True, **upload_mgr.get_state()})


@app.route("/api/cc/upload/start", methods=["POST"])
def cc_upload_start():
    data = request.get_json(silent=True) or {}
    env = str(data.get("env") or "esp32wroom")
    mode = str(data.get("mode") or "ota")
    target = data.get("target")
    try:
        res = upload_mgr.start_upload(env, mode, target=target)
        event_db.log_audit("admin", "cc_upload_start", f"{env}:{mode}", str(target or "default"))
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


if __name__ == "__main__":
    CC_PORT = int(os.environ.get("CONTROL_CENTER_PORT", 5001))
    print("=" * 65)
    print(f"[*] Smart Entry Control Center running on http://0.0.0.0:{CC_PORT}")
    print("=" * 65)
    app.run(host="0.0.0.0", port=CC_PORT, debug=False, threaded=True)
