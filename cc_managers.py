"""
Control Center Managers (D3-D7):
- D3: WorkspaceManager (releases/, workspace/, builds/, config/, strict whitelist)
- D4: ReleaseManager (GitHub release check, download, zip-slip verify, install, rollback)
- D5: Controlled file tree, read, save (.bak snapshot), revert
- D6: BuildManager (PlatformIO esp32cam / esp32wroom build, clean, config audit, artifacts)
- D7: UploadManager (authoritative scripts/ota_upload.py for OTA + pio for USB serial + verify)
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
RELEASES_DIR = os.path.join(ROOT_DIR, "releases")
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")
WORKSPACE_FW_DIR = os.path.join(WORKSPACE_DIR, "firmware", "esp32-cam")
SOURCE_FW_DIR = os.path.join(ROOT_DIR, "firmware", "esp32-cam")
ARTIFACTS_DIR = os.path.join(ROOT_DIR, "builds")
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
CC_CONFIG_PATH = os.path.join(CONFIG_DIR, "cc_config.json")
OTA_SCRIPT_PATH = os.path.join(ROOT_DIR, "scripts", "ota_upload.py")

ALLOWED_EDIT_EXTS = {".h", ".hpp", ".c", ".cpp", ".ini", ".json"}
IGNORED_DIRS = {".pio", ".git", "__pycache__", ".vscode", ".backups"}
ALLOWED_ENVS = {
    "esp32cam": {"label": "ESP32-CAM", "default_ip_key": "esp32_cam_ip"},
    "esp32wroom": {"label": "ESP32-WROOM", "default_ip_key": "esp32_wroom_ip"},
}


def find_pio_executable():
    pio = shutil.which("pio")
    if pio:
        return pio
    candidates = [
        os.path.join(os.path.expanduser("~"), ".platformio", "penv", "Scripts", "pio.exe"),
        os.path.join(os.path.expanduser("~"), ".platformio", "penv", "bin", "pio"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def load_cc_config():
    defaults = {
        "github_repo": os.environ.get("GITHUB_REPO", "AdhyatmDeharia/Smart-Door-Entry-Detection").strip(),
        "esp32_cam_ip": os.environ.get("ESP32_CAM_IP", "192.168.29.10").strip(),
        "esp32_wroom_ip": os.environ.get("ESP32_WROOM_IP", "192.168.29.20").strip(),
        "installed_version": "local-workspace",
        "last_release_check": None,
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CC_CONFIG_PATH):
        try:
            with open(CC_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            pass
    return defaults


def save_cc_config(patch: dict):
    cfg = load_cc_config()
    for k in ("github_repo", "esp32_cam_ip", "esp32_wroom_ip", "installed_version", "last_release_check"):
        if k in patch and patch[k] is not None:
            cfg[k] = str(patch[k]).strip() if k != "last_release_check" else patch[k]
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CC_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


class WorkspaceManager:
    """D3 & D5: Safe workspace directory, file tree, and whitelisted editor operations."""

    def __init__(self):
        self._lock = threading.Lock()
        self.ensure_initialized()

    def ensure_initialized(self):
        for d in (RELEASES_DIR, WORKSPACE_DIR, ARTIFACTS_DIR, CONFIG_DIR):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(os.path.join(WORKSPACE_FW_DIR, "platformio.ini")) and os.path.exists(SOURCE_FW_DIR):
            self.seed_from_source()

    def seed_from_source(self):
        with self._lock:
            os.makedirs(WORKSPACE_FW_DIR, exist_ok=True)
            for root, dirs, files in os.walk(SOURCE_FW_DIR):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                rel_dir = os.path.relpath(root, SOURCE_FW_DIR)
                dest_dir = WORKSPACE_FW_DIR if rel_dir == "." else os.path.join(WORKSPACE_FW_DIR, rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                for fn in files:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in ALLOWED_EDIT_EXTS or fn == "min_spiffs.csv":
                        shutil.copy2(os.path.join(root, fn), os.path.join(dest_dir, fn))

    def _resolve_safe_path(self, rel_path: str) -> str:
        if not rel_path or "\x00" in rel_path or rel_path.startswith(("/", "\\")):
            raise ValueError("Invalid relative path")
        norm = rel_path.replace("\\", "/").strip("/")
        if ".." in norm.split("/"):
            raise ValueError("Path traversal blocked")
        base_real = os.path.realpath(WORKSPACE_FW_DIR)
        target_real = os.path.realpath(os.path.join(base_real, norm))
        if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
            raise ValueError("Path escapes workspace")
        ext = os.path.splitext(target_real)[1].lower()
        if ext not in ALLOWED_EDIT_EXTS:
            raise ValueError(f"File type '{ext}' is not in the editable whitelist")
        return target_real

    def list_tree(self):
        self.ensure_initialized()
        files = []
        base_real = os.path.realpath(WORKSPACE_FW_DIR)
        for root, dirs, names in os.walk(base_real):
            dirs[:] = sorted([d for d in dirs if d not in IGNORED_DIRS])
            for fn in sorted(names):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in ALLOWED_EDIT_EXTS:
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, base_real).replace("\\", "/")
                has_bak = os.path.exists(full + ".bak")
                files.append({
                    "path": rel,
                    "ext": ext.lstrip("."),
                    "size": os.path.getsize(full),
                    "has_backup": has_bak,
                })
        return files

    def read_file(self, rel_path: str):
        full = self._resolve_safe_path(rel_path)
        if not os.path.isfile(full):
            raise FileNotFoundError(rel_path)
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {
            "path": rel_path.replace("\\", "/"),
            "content": content,
            "has_backup": os.path.exists(full + ".bak"),
            "size": len(content.encode("utf-8")),
        }

    def save_file(self, rel_path: str, content: str):
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ValueError("File exceeds 512 KB editor limit")
        full = self._resolve_safe_path(rel_path)
        if not os.path.isfile(full):
            raise FileNotFoundError(rel_path)
        with self._lock:
            shutil.copy2(full, full + ".bak")
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        return {"path": rel_path.replace("\\", "/"), "saved": True, "has_backup": True}

    def revert_file(self, rel_path: str):
        full = self._resolve_safe_path(rel_path)
        bak = full + ".bak"
        if not os.path.isfile(bak):
            raise FileNotFoundError("No backup snapshot exists for this file")
        with self._lock:
            shutil.copy2(bak, full)
        return self.read_file(rel_path)


class ReleaseManager:
    """D4: GitHub release checker, safe zip downloader, installer, and rollback."""

    def __init__(self, workspace_mgr: WorkspaceManager):
        self.workspace_mgr = workspace_mgr
        self._lock = threading.Lock()

    def _github_get_json(self, url: str):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "SmartDoor-ControlCenter/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def check_latest(self, repo: str | None = None):
        cfg = load_cc_config()
        repo = (repo or cfg["github_repo"]).strip().strip("/")
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            raise ValueError("Invalid GitHub repo format (expected owner/repo)")

        info = {
            "repo": repo,
            "installed_version": cfg.get("installed_version", "local-workspace"),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            rel = self._github_get_json(f"https://api.github.com/repos/{repo}/releases/latest")
            info.update({
                "ok": True,
                "kind": "release",
                "tag": rel.get("tag_name") or rel.get("name") or "latest",
                "name": rel.get("name") or rel.get("tag_name"),
                "published_at": rel.get("published_at"),
                "zip_url": rel.get("zipball_url"),
                "html_url": rel.get("html_url"),
                "notes": (rel.get("body") or "")[:600],
            })
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # Fallback to default branch zipball when repo has no formal GitHub Release yet
            repo_meta = self._github_get_json(f"https://api.github.com/repos/{repo}")
            branch = repo_meta.get("default_branch", "main")
            commit = self._github_get_json(f"https://api.github.com/repos/{repo}/commits/{branch}")
            sha = (commit.get("sha") or "")[:7]
            info.update({
                "ok": True,
                "kind": "branch",
                "tag": f"{branch}@{sha}" if sha else branch,
                "name": f"Branch {branch} ({sha})",
                "published_at": commit.get("commit", {}).get("committer", {}).get("date"),
                "zip_url": f"https://github.com/{repo}/archive/refs/heads/{branch}.zip",
                "html_url": f"https://github.com/{repo}",
                "notes": commit.get("commit", {}).get("message", "")[:400],
            })

        save_cc_config({"last_release_check": info})
        return info

    def list_backups(self):
        os.makedirs(RELEASES_DIR, exist_ok=True)
        items = []
        for name in sorted(os.listdir(RELEASES_DIR), reverse=True):
            p = os.path.join(RELEASES_DIR, name)
            if os.path.isdir(p) and name.startswith("backup_"):
                items.append({"name": name, "ts": name.replace("backup_", "")})
        return items

    def install_latest(self, repo: str | None = None):
        latest = self.check_latest(repo)
        zip_url = latest.get("zip_url")
        if not zip_url or not zip_url.startswith("https://"):
            raise ValueError("Invalid release download URL")

        req = urllib.request.Request(zip_url, headers={"User-Agent": "SmartDoor-ControlCenter/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_bytes = resp.read(50 * 1024 * 1024 + 1)
        if len(raw_bytes) > 50 * 1024 * 1024:
            raise ValueError("Release archive exceeds 50 MB limit")

        archive_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stage_dir = os.path.join(RELEASES_DIR, f"_stage_{ts}")
        backup_dir = os.path.join(RELEASES_DIR, f"backup_{ts}")

        with self._lock:
            os.makedirs(stage_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    for member in zf.infolist():
                        norm = member.filename.replace("\\", "/")
                        if norm.startswith("/") or ".." in norm.split("/"):
                            raise ValueError(f"Unsafe path in archive: {member.filename}")
                        dest = os.path.realpath(os.path.join(stage_dir, norm))
                        if not dest.startswith(os.path.realpath(stage_dir) + os.sep) and dest != os.path.realpath(stage_dir):
                            raise ValueError("Zip-slip detected")
                    zf.extractall(stage_dir)

                # Locate firmware/esp32-cam inside extracted root folder
                top_entries = [os.path.join(stage_dir, e) for e in os.listdir(stage_dir)]
                extract_root = top_entries[0] if len(top_entries) == 1 and os.path.isdir(top_entries[0]) else stage_dir
                new_fw = os.path.join(extract_root, "firmware", "esp32-cam")
                if not os.path.isfile(os.path.join(new_fw, "platformio.ini")):
                    raise ValueError("Downloaded archive does not contain firmware/esp32-cam/platformio.ini")

                # Backup current workspace firmware before replacing
                if os.path.exists(WORKSPACE_FW_DIR):
                    shutil.copytree(WORKSPACE_FW_DIR, backup_dir, ignore=shutil.ignore_patterns(".pio", "*.bin", "*.elf"))

                # Preserve local secrets.h / wifi_secrets.h if present in workspace
                preserved = {}
                for sec_name in ("secrets.h", "wifi_secrets.h"):
                    sec_p = os.path.join(WORKSPACE_FW_DIR, "include", sec_name)
                    if os.path.isfile(sec_p):
                        with open(sec_p, "rb") as sf:
                            preserved[sec_name] = sf.read()

                shutil.rmtree(WORKSPACE_FW_DIR, ignore_errors=True)
                shutil.copytree(new_fw, WORKSPACE_FW_DIR, ignore=shutil.ignore_patterns(".pio", "*.bin", "*.elf"))
                for sec_name, data in preserved.items():
                    with open(os.path.join(WORKSPACE_FW_DIR, "include", sec_name), "wb") as sf:
                        sf.write(data)

                save_cc_config({"installed_version": latest["tag"]})
                return {
                    "ok": True,
                    "installed_version": latest["tag"],
                    "sha256": archive_sha256,
                    "backup": os.path.basename(backup_dir),
                }
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)

    def rollback(self, backup_name: str | None = None):
        backups = self.list_backups()
        if not backups:
            raise FileNotFoundError("No rollback backup available")
        chosen = backup_name or backups[0]["name"]
        if not re.match(r"^backup_[0-9_]+$", chosen):
            raise ValueError("Invalid backup name")
        src = os.path.join(RELEASES_DIR, chosen)
        if not os.path.isdir(src):
            raise FileNotFoundError(chosen)
        with self._lock:
            shutil.rmtree(WORKSPACE_FW_DIR, ignore_errors=True)
            shutil.copytree(src, WORKSPACE_FW_DIR)
            save_cc_config({"installed_version": f"rollback:{chosen}"})
        return {"ok": True, "restored": chosen}


class BuildManager:
    """D6: Controlled PlatformIO builder, cleaner, config auditor, and artifact store."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"
        self.active_env = None
        self.last_exit_code = None
        self.last_finished_at = None
        self.logs = []

    def _append_log(self, line: str):
        self.logs.append(line.rstrip("\r\n"))
        if len(self.logs) > 400:
            self.logs = self.logs[-400:]

    def list_artifacts(self):
        out = []
        for env in ALLOWED_ENVS:
            bin_p = os.path.join(ARTIFACTS_DIR, env, "firmware.bin")
            if os.path.isfile(bin_p):
                with open(bin_p, "rb") as f:
                    data = f.read()
                out.append({
                    "env": env,
                    "size": len(data),
                    "md5": hashlib.md5(data).hexdigest(),
                    "sha256": hashlib.sha256(data).hexdigest()[:16],
                    "mtime": datetime.fromtimestamp(os.path.getmtime(bin_p)).isoformat(timespec="seconds"),
                })
        return out

    def get_state(self):
        return {
            "status": self.status,
            "active_env": self.active_env,
            "last_exit_code": self.last_exit_code,
            "last_finished_at": self.last_finished_at,
            "pio_available": bool(find_pio_executable()),
            "logs": list(self.logs[-150:]),
            "artifacts": self.list_artifacts(),
        }

    def run_audit(self):
        findings = []
        sec_path = os.path.join(WORKSPACE_FW_DIR, "include", "secrets.h")
        wifi_sec = os.path.join(WORKSPACE_FW_DIR, "include", "wifi_secrets.h")
        if os.path.isfile(sec_path):
            with open(sec_path, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            if "YOUR_WIFI_SSID" in txt and not os.path.isfile(wifi_sec):
                findings.append({"level": "warn", "msg": "Wi-Fi SSID/password still uses placeholder in secrets.h (and wifi_secrets.h is absent)."})
            if '"admin1234"' in txt or '"user1234"' in txt:
                findings.append({"level": "warn", "msg": "Dashboard login still uses default placeholder password in secrets.h."})
        else:
            findings.append({"level": "error", "msg": "Missing include/secrets.h in workspace."})

        cfg_path = os.path.join(WORKSPACE_FW_DIR, "include", "configs.h")
        if os.path.isfile(cfg_path):
            findings.append({"level": "ok", "msg": "configs.h present."})
        if os.path.isfile(os.path.join(WORKSPACE_FW_DIR, "platformio.ini")):
            findings.append({"level": "ok", "msg": "platformio.ini present."})
        return {"ok": True, "findings": findings, "pio_available": bool(find_pio_executable())}

    def start_build(self, env: str, clean: bool = False):
        if env not in ALLOWED_ENVS:
            raise ValueError(f"Unsupported environment: {env}")
        pio = find_pio_executable()
        if not pio:
            raise RuntimeError("PlatformIO ('pio') not found in container/host")

        with self._lock:
            if self.status == "running":
                raise RuntimeError("A build is already running")
            self.status = "running"
            self.active_env = env
            self.last_exit_code = None
            self.logs = [f"=== Starting {'clean' if clean else 'build'} for {env} ({datetime.now().strftime('%H:%M:%S')}) ==="]

        def _worker():
            try:
                cmd = [pio, "run", "-d", WORKSPACE_FW_DIR, "-e", env]
                if clean:
                    cmd.extend(["-t", "clean"])
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
                for line in proc.stdout:
                    self._append_log(line)
                rc = proc.wait()
                self.last_exit_code = rc
                if rc == 0 and not clean:
                    built_bin = os.path.join(WORKSPACE_FW_DIR, ".pio", "build", env, "firmware.bin")
                    if os.path.isfile(built_bin):
                        dest_dir = os.path.join(ARTIFACTS_DIR, env)
                        os.makedirs(dest_dir, exist_ok=True)
                        shutil.copy2(built_bin, os.path.join(dest_dir, "firmware.bin"))
                        self._append_log(f"[+] Artifact saved to builds/{env}/firmware.bin")
                    self.status = "success"
                elif rc == 0:
                    self.status = "success"
                else:
                    self.status = "failed"
            except Exception as e:
                self._append_log(f"[ERROR] {e}")
                self.last_exit_code = -1
                self.status = "failed"
            finally:
                self.last_finished_at = datetime.now().isoformat(timespec="seconds")

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "status": "running", "env": env}


class UploadManager:
    """D7: Authoritative scripts/ota_upload.py OTA + USB Serial upload + /status verify."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"
        self.last_exit_code = None
        self.last_verified = None
        self.logs = []

    def _append_log(self, line: str):
        self.logs.append(line.rstrip("\r\n"))
        if len(self.logs) > 300:
            self.logs = self.logs[-300:]

    def list_serial_ports(self):
        pio = find_pio_executable()
        if not pio:
            return []
        try:
            out = subprocess.check_output([pio, "device", "list", "--json-output"], text=True, timeout=8)
            ports = json.loads(out)
            return [{"port": p.get("port"), "description": p.get("description", "")} for p in ports if p.get("port")]
        except Exception:
            return []

    def get_state(self):
        return {
            "status": self.status,
            "last_exit_code": self.last_exit_code,
            "last_verified": self.last_verified,
            "logs": list(self.logs[-150:]),
            "serial_ports": self.list_serial_ports(),
        }

    def start_upload(self, env: str, mode: str, target: str | None = None):
        if env not in ALLOWED_ENVS:
            raise ValueError("Unsupported environment")
        if mode not in ("ota", "serial"):
            raise ValueError("Upload mode must be 'ota' or 'serial'")

        cfg = load_cc_config()
        if mode == "ota":
            ip = (target or cfg[ALLOWED_ENVS[env]["default_ip_key"]]).strip()
            if not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", ip):
                raise ValueError("Invalid IPv4 address for OTA")
            bin_path = os.path.join(ARTIFACTS_DIR, env, "firmware.bin")
            if not os.path.isfile(bin_path):
                bin_path = os.path.join(WORKSPACE_FW_DIR, ".pio", "build", env, "firmware.bin")
            if not os.path.isfile(bin_path):
                raise FileNotFoundError(f"No built firmware.bin for {env}. Build first.")
            cmd = [sys.executable, OTA_SCRIPT_PATH, "-i", ip, "-f", bin_path]
            verify_ip = ip
        else:
            port = (target or "").strip()
            if not port or not re.match(r"^(COM\d+|/dev/[A-Za-z0-9._-]+)$", port):
                raise ValueError("Invalid serial port")
            pio = find_pio_executable()
            if not pio:
                raise RuntimeError("PlatformIO ('pio') not found")
            cmd = [pio, "run", "-d", WORKSPACE_FW_DIR, "-e", env, "-t", "upload", "--upload-port", port]
            verify_ip = None

        with self._lock:
            if self.status == "running":
                raise RuntimeError("An upload is already running")
            self.status = "running"
            self.last_exit_code = None
            self.last_verified = None
            self.logs = [f"=== Starting {mode.upper()} upload for {env} -> {target or verify_ip} ==="]

        def _worker():
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
                for line in proc.stdout:
                    self._append_log(line)
                rc = proc.wait()
                self.last_exit_code = rc
                if rc == 0:
                    self.status = "success"
                    if verify_ip:
                        self._append_log(f"[*] Waiting 4s to verify http://{verify_ip}/status ...")
                        time.sleep(4.0)
                        try:
                            with urllib.request.urlopen(f"http://{verify_ip}/status", timeout=4) as r:
                                st = json.loads(r.read().decode("utf-8", errors="ignore"))
                            self.last_verified = {"ok": True, "ip": verify_ip, "uptime_s": st.get("uptime_s"), "mode": st.get("mode")}
                            self._append_log(f"[+] Verified online: mode={st.get('mode')}, uptime={st.get('uptime_s')}s")
                        except Exception as ve:
                            self.last_verified = {"ok": False, "ip": verify_ip, "error": str(ve)}
                            self._append_log(f"[!] Post-OTA /status check failed: {ve}")
                else:
                    self.status = "failed"
            except Exception as e:
                self._append_log(f"[ERROR] {e}")
                self.last_exit_code = -1
                self.status = "failed"

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "status": "running", "env": env, "mode": mode}
