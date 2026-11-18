"""
Phase 9: Persistent SQLite Event Database, Audit Log & Analytics
=================================================================
Stores all recognition events and user/system audit actions in `data/events.db`
using SQLite WAL mode for concurrent read/write safety.
"""

import os
import json
import time
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta

DB_PATH = os.path.join("data", "events.db")


class EventDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_unusable_log_ts = 0.0
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self):
        with self._lock:
            if os.path.exists(self.db_path):
                try:
                    qc = self._connect()
                    res = qc.execute("PRAGMA quick_check;").fetchone()
                    qc.close()
                    if not res or str(res[0]).lower() != "ok":
                        raise sqlite3.DatabaseError(f"quick_check failed: {res}")
                except sqlite3.DatabaseError:
                    corrupt_path = f"{self.db_path}.corrupt.{int(time.time())}"
                    try:
                        os.replace(self.db_path, corrupt_path)
                        print(f"[!] EventDB corruption detected -> quarantined to {corrupt_path}")
                    except OSError:
                        pass
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS recognition_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        epoch REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        person_id TEXT,
                        person_label TEXT,
                        similarity REAL DEFAULT 0.0,
                        quality REAL DEFAULT 0.0,
                        reason TEXT,
                        thumb TEXT,
                        objects_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_epoch ON recognition_events(epoch DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_type ON recognition_events(event_type);
                    CREATE INDEX IF NOT EXISTS idx_events_person ON recognition_events(person_id);

                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        epoch REAL NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT,
                        details TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_epoch ON audit_log(epoch DESC);

                    CREATE TABLE IF NOT EXISTS hash_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        epoch REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        person_id TEXT,
                        payload_json TEXT NOT NULL,
                        prev_hash TEXT NOT NULL,
                        entry_hash TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ledger_id ON hash_ledger(id DESC);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def backfill_from_detections(self, person_detections, person_labels):
        """One-time migration from legacy detections.json if recognition_events is empty."""
        if not person_detections:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM recognition_events").fetchone()[0]
                if count > 0:
                    return 0
                rows = []
                for pid, timestamps in person_detections.items():
                    label = person_labels.get(pid)
                    for dt in timestamps:
                        if isinstance(dt, datetime):
                            ts_iso = dt.isoformat()
                            ep = dt.timestamp()
                        else:
                            ts_iso = str(dt)
                            ep = time.time()
                        rows.append((
                            ts_iso, ep, "recognized", pid, label,
                            1.0, 50.0, "Migrated from detections.json", None, None
                        ))
                if rows:
                    conn.executemany(
                        """INSERT INTO recognition_events
                           (ts, epoch, event_type, person_id, person_label, similarity, quality, reason, thumb, objects_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                    conn.commit()
                return len(rows)
            finally:
                conn.close()

    def log_recognition_event(self, event_type, person_id=None, person_label=None,
                              similarity=0.0, quality=0.0, reason="", thumb=None, objects=None):
        if event_type == "no_face":
            return
        now = time.time()
        # Throttle consecutive low-quality noise so DB stays clean and fast
        if event_type == "unusable_quality":
            if (now - self._last_unusable_log_ts) < 5.0:
                return
            self._last_unusable_log_ts = now

        ts_iso = datetime.now().isoformat(timespec="seconds")
        obj_json = json.dumps(objects) if objects else None

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO recognition_events
                       (ts, epoch, event_type, person_id, person_label, similarity, quality, reason, thumb, objects_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts_iso, now, event_type, person_id, person_label,
                     round(float(similarity), 3), round(float(quality), 1),
                     reason, thumb, obj_json),
                )
                self._append_ledger_locked(conn, ts_iso, now, event_type, person_id, {
                    "label": person_label,
                    "similarity": round(float(similarity), 3),
                    "quality": round(float(quality), 1),
                    "reason": reason,
                    "thumb": thumb,
                })
                conn.commit()
            finally:
                conn.close()

    def _append_ledger_locked(self, conn, ts_iso, epoch_val, event_type, person_id, payload_dict):
        row = conn.execute("SELECT entry_hash FROM hash_ledger ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = row[0] if row else ("0" * 64)
        payload_json = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
        raw = f"{prev_hash}|{ts_iso}|{event_type}|{person_id or ''}|{payload_json}".encode("utf-8")
        entry_hash = hashlib.sha256(raw).hexdigest()
        conn.execute(
            """INSERT INTO hash_ledger (ts, epoch, event_type, person_id, payload_json, prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts_iso, epoch_val, str(event_type), str(person_id or ""), payload_json, prev_hash, entry_hash),
        )
        return entry_hash

    def verify_ledger_integrity(self):
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, ts, event_type, person_id, payload_json, prev_hash, entry_hash FROM hash_ledger ORDER BY id ASC"
                ).fetchall()
                expected_prev = "0" * 64
                latest_hash = expected_prev
                for r in rows:
                    if r["prev_hash"] != expected_prev:
                        return {"ok": False, "entries": len(rows), "broken_at_id": r["id"], "latest_hash": latest_hash}
                    raw = f"{r['prev_hash']}|{r['ts']}|{r['event_type']}|{r['person_id'] or ''}|{r['payload_json']}".encode("utf-8")
                    calc = hashlib.sha256(raw).hexdigest()
                    if calc != r["entry_hash"]:
                        return {"ok": False, "entries": len(rows), "broken_at_id": r["id"], "latest_hash": latest_hash}
                    expected_prev = calc
                    latest_hash = calc
                return {"ok": True, "entries": len(rows), "broken_at_id": None, "latest_hash": latest_hash}
            finally:
                conn.close()

    def log_audit(self, actor, action, target="", details=""):
        now = time.time()
        ts_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO audit_log (ts, epoch, actor, action, target, details) VALUES (?, ?, ?, ?, ?, ?)",
                    (ts_iso, now, actor, action, str(target or ""), str(details or "")),
                )
                self._append_ledger_locked(conn, ts_iso, now, f"AUDIT:{action}", actor, {
                    "target": str(target or ""),
                    "details": str(details or ""),
                })
                conn.commit()
            finally:
                conn.close()

    def query_events(self, limit=50, offset=0, event_type=None, person_id=None, search=None):
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        clauses = []
        params = []
        if event_type and event_type != "all":
            clauses.append("event_type = ?")
            params.append(event_type)
        if person_id:
            clauses.append("person_id = ?")
            params.append(person_id)
        if search:
            clauses.append("(person_id LIKE ? OR person_label LIKE ? OR reason LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(f"SELECT COUNT(*) FROM recognition_events {where_sql}", params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT id, ts, epoch, event_type, person_id, person_label, similarity, quality, reason, thumb, objects_json
                        FROM recognition_events {where_sql}
                        ORDER BY epoch DESC, id DESC LIMIT ? OFFSET ?""",
                    params + [limit, offset],
                ).fetchall()
                items = []
                for r in rows:
                    d = dict(r)
                    d["objects"] = json.loads(d["objects_json"]) if d.get("objects_json") else []
                    d.pop("objects_json", None)
                    items.append(d)
                return {"total": total, "items": items}
            finally:
                conn.close()

    def query_audit(self, limit=50, offset=0):
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
                rows = conn.execute(
                    "SELECT id, ts, epoch, actor, action, target, details FROM audit_log ORDER BY epoch DESC, id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                return {"total": total, "items": [dict(r) for r in rows]}
            finally:
                conn.close()

    def get_analytics(self, days=7):
        days = max(1, min(90, int(days)))
        cutoff = time.time() - (days * 86400)
        cutoff_24h = time.time() - 86400
        with self._lock:
            conn = self._connect()
            try:
                by_type_rows = conn.execute(
                    "SELECT event_type, COUNT(*) AS cnt FROM recognition_events WHERE epoch >= ? GROUP BY event_type",
                    (cutoff,),
                ).fetchall()
                by_type = {r["event_type"]: r["cnt"] for r in by_type_rows}

                top_rows = conn.execute(
                    """SELECT person_id, COALESCE(MAX(person_label), person_id) AS label, COUNT(*) AS cnt, MAX(ts) AS last_seen
                       FROM recognition_events
                       WHERE event_type = 'recognized' AND person_id IS NOT NULL AND epoch >= ?
                       GROUP BY person_id ORDER BY cnt DESC LIMIT 10""",
                    (cutoff,),
                ).fetchall()
                top_persons = [dict(r) for r in top_rows]

                hourly_rows = conn.execute(
                    """SELECT strftime('%H', ts) AS hr, COUNT(*) AS cnt
                       FROM recognition_events
                       WHERE epoch >= ? AND event_type = 'recognized'
                       GROUP BY hr ORDER BY hr""",
                    (cutoff_24h,),
                ).fetchall()
                hourly = {f"{h:02d}": 0 for h in range(24)}
                for r in hourly_rows:
                    if r["hr"] is not None:
                        hourly[r["hr"]] = r["cnt"]

                daily_rows = conn.execute(
                    """SELECT substr(ts, 1, 10) AS day, COUNT(*) AS cnt
                       FROM recognition_events
                       WHERE epoch >= ? AND event_type = 'recognized'
                       GROUP BY day ORDER BY day""",
                    (cutoff,),
                ).fetchall()
                daily = {r["day"]: r["cnt"] for r in daily_rows if r["day"]}

                total_all = conn.execute("SELECT COUNT(*) FROM recognition_events").fetchone()[0]
                total_audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

                return {
                    "days": days,
                    "total_events": total_all,
                    "total_audit_entries": total_audit,
                    "by_type": by_type,
                    "top_persons": top_persons,
                    "hourly_24h": hourly,
                    "daily": daily,
                }
            finally:
                conn.close()

    def clear_all(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM recognition_events")
                conn.commit()
            finally:
                conn.close()
        self.log_audit("user", "reset", "all_data", "Cleared stored faces, detections, and recognition_events")


event_db = EventDatabase()
