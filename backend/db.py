"""
SQLite 存储层：任务、任务事件、术语表、设置。

单进程使用，所有访问经过一把锁串行化，足够应付本地服务的并发量。
"""
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT,
    source_type TEXT NOT NULL,
    source_url TEXT,
    source_file TEXT,
    clip_start REAL,
    clip_end REAL,
    team1 TEXT,
    team2 TEXT,
    match_hltv_id TEXT,
    match_time REAL,
    notes TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    stage TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);

CREATE TABLE IF NOT EXISTS glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- alias: 别名 → HLTV 规范名；term: 英文术语 → 中文译法
    term TEXT NOT NULL,
    target TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""

# 首次启动时写入术语表的常见战队简称 → HLTV 规范名
DEFAULT_ALIASES = [
    ("NAVI", "Natus Vincere"), ("Na'Vi", "Natus Vincere"), ("Navi", "Natus Vincere"),
    ("VP", "Virtus.pro"), ("Mongolz", "The MongolZ"),
    ("小蜜蜂", "Vitality"), ("天禄", "TYLOO"),
]

# 旧库升级时补上的列：列名 → 定义
MIGRATIONS = {
    "tasks": {"match_time": "REAL"},
}

TASK_UPDATABLE = {
    "title", "state", "stage", "source_type", "source_url", "source_file",
    "clip_start", "clip_end", "team1", "team2", "match_hltv_id", "match_time", "notes",
    "context", "error", "cancel_requested",
}

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init_db() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        if _conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0] == 0:
            _conn.executemany(
                "INSERT INTO glossary (kind, term, target, note) VALUES ('alias', ?, ?, '预置')",
                DEFAULT_ALIASES,
            )
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in _conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库未初始化")
    return _conn


def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
    task = dict(row)
    task["context"] = json.loads(task.pop("context_json") or "{}")
    task["cancel_requested"] = bool(task["cancel_requested"])
    return task


# ── 任务 ──────────────────────────────────────────────────────────────────────

def create_task(**fields: Any) -> Dict[str, Any]:
    now = time.time()
    context = fields.pop("context", {}) or {}
    columns = ["created_at", "updated_at", "context_json"] + list(fields)
    values = [now, now, json.dumps(context, ensure_ascii=False)] + list(fields.values())
    placeholders = ", ".join("?" for _ in columns)
    with _lock:
        cur = _db().execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        _db().commit()
        task_id = cur.lastrowid
    return get_task(task_id)


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _task_row(row) if row else None


def list_tasks() -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    return [_task_row(r) for r in rows]


def find_task_by_match(match_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _db().execute(
            "SELECT * FROM tasks WHERE match_hltv_id = ? AND state != 'cancelled' ORDER BY id DESC LIMIT 1",
            (str(match_id),),
        ).fetchone()
    return _task_row(row) if row else None


def update_task(task_id: int, **fields: Any) -> Dict[str, Any]:
    unknown = set(fields) - TASK_UPDATABLE
    if unknown:
        raise ValueError(f"不可更新的字段: {unknown}")
    if "context" in fields:
        fields["context_json"] = json.dumps(fields.pop("context") or {}, ensure_ascii=False)
    if "cancel_requested" in fields:
        fields["cancel_requested"] = int(bool(fields["cancel_requested"]))
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        _db().execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", list(fields.values()) + [task_id]
        )
        _db().commit()
    return get_task(task_id)


def delete_task(task_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        _db().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        _db().commit()


def add_event(task_id: int, message: str, level: str = "info", stage: Optional[str] = None) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO task_events (task_id, ts, level, stage, message) VALUES (?, ?, ?, ?, ?)",
            (task_id, time.time(), level, stage, message),
        )
        _db().commit()


def list_events(task_id: int) -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── 术语表 ────────────────────────────────────────────────────────────────────

def list_glossary() -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM glossary ORDER BY kind, term").fetchall()
    return [dict(r) for r in rows]


def upsert_glossary(entry: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        if entry.get("id"):
            _db().execute(
                "UPDATE glossary SET kind = ?, term = ?, target = ?, note = ? WHERE id = ?",
                (entry["kind"], entry["term"], entry["target"], entry.get("note"), entry["id"]),
            )
            entry_id = entry["id"]
        else:
            cur = _db().execute(
                "INSERT INTO glossary (kind, term, target, note) VALUES (?, ?, ?, ?)",
                (entry["kind"], entry["term"], entry["target"], entry.get("note")),
            )
            entry_id = cur.lastrowid
        _db().commit()
        row = _db().execute("SELECT * FROM glossary WHERE id = ?", (entry_id,)).fetchone()
    return dict(row)


def delete_glossary(entry_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM glossary WHERE id = ?", (entry_id,))
        _db().commit()


# ── 设置 ──────────────────────────────────────────────────────────────────────

def get_settings() -> Dict[str, Any]:
    with _lock:
        row = _db().execute("SELECT value_json FROM settings WHERE key = 'app'").fetchone()
    stored = json.loads(row["value_json"]) if row else {}
    return config.merge_settings(config.DEFAULT_SETTINGS, stored)


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = config.merge_settings(config.DEFAULT_SETTINGS, settings)
    with _lock:
        _db().execute(
            "INSERT INTO settings (key, value_json) VALUES ('app', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (json.dumps(merged, ensure_ascii=False),),
        )
        _db().commit()
    return merged
