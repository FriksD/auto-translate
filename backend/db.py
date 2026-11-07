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

-- 飞书群里每个任务的那张进度卡片
CREATE TABLE IF NOT EXISTS feishu_cards (
    task_id INTEGER PRIMARY KEY,
    message_id TEXT,             -- 还没发出时为空
    sent_at REAL,
    digest TEXT,                 -- 上次发出的卡片内容摘要，没变化就不更新
    rendered_at REAL,
    reserved_by TEXT             -- 在飞书里预约的人（open_id）
);

-- 直播间监视：一个赛事一个，最多两条直播链接
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    event_id TEXT,
    event_name TEXT,
    streams_json TEXT NOT NULL DEFAULT '[]',   -- [{url, label, proxy}]
    enabled INTEGER NOT NULL DEFAULT 0,        -- 手动开着
    windows_json TEXT NOT NULL DEFAULT '[]',   -- 自动监视时间窗 [{start, end, match_ids}]
    follow_schedule INTEGER NOT NULL DEFAULT 0, -- 按赛事赛程自动算时间窗（预约过这个赛事的比赛）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_events_monitor ON monitor_events(monitor_id);

-- 飞书群里每个直播间的那张卡片
CREATE TABLE IF NOT EXISTS monitor_cards (
    monitor_id INTEGER PRIMARY KEY,
    message_id TEXT,
    sent_at REAL,
    digest TEXT
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

# 旧的「预约比赛 → 看比分 → 录制」流程的任务状态：改成直播间监视后不再有，旧任务迁移成已取消
RETIRED_STATES = ("reserved", "watching", "recording", "interview_detecting", "no_interview")

# 旧库升级时补上的列：列名 → 定义
MIGRATIONS = {
    "tasks": {"match_time": "REAL"},
    "feishu_cards": {"reserved_by": "TEXT"},
    "monitors": {"follow_schedule": "INTEGER NOT NULL DEFAULT 0"},
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
        # 旧的按场预约时间窗：改成按赛程监视整个赛事
        _conn.execute("UPDATE monitors SET follow_schedule = 1 WHERE windows_json LIKE '%\"match_id\"%'")
        marks = ", ".join("?" for _ in RETIRED_STATES)
        now = time.time()
        for row in _conn.execute(f"SELECT id FROM tasks WHERE state IN ({marks})", RETIRED_STATES).fetchall():
            _conn.execute("INSERT INTO task_events (task_id, ts, level, stage, message) VALUES (?, ?, 'warning', NULL, ?)",
                          (row["id"], now, "预约比赛的流程已改成直播间监视，这个任务已取消；采访会从直播间截取成新任务"))
        _conn.execute(f"UPDATE tasks SET state = 'cancelled', updated_at = ? WHERE state IN ({marks})",
                      (now, *RETIRED_STATES))
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
        _db().execute("DELETE FROM feishu_cards WHERE task_id = ?", (task_id,))
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


def recent_events(task_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """最近的若干条事件，按时间正序。"""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── 飞书卡片 ──────────────────────────────────────────────────────────────────

def track_card(task_id: int, reserved_by: Optional[str] = None) -> None:
    """登记任务需要一张飞书卡片（由卡片服务发出）；已登记的不变。"""
    with _lock:
        _db().execute("INSERT OR IGNORE INTO feishu_cards (task_id, reserved_by) VALUES (?, ?)",
                      (task_id, reserved_by))
        _db().commit()


def get_card(task_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _db().execute("SELECT * FROM feishu_cards WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_cards() -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM feishu_cards ORDER BY task_id").fetchall()
    return [dict(r) for r in rows]


def update_card(task_id: int, **fields: Any) -> None:
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        _db().execute(f"UPDATE feishu_cards SET {assignments} WHERE task_id = ?", list(fields.values()) + [task_id])
        _db().commit()


def invalidate_cards() -> None:
    """让所有卡片下次都重算一遍（例如卡片上的链接地址变了）。"""
    with _lock:
        _db().execute("UPDATE feishu_cards SET rendered_at = NULL")
        _db().commit()


# ── 直播间监视 ────────────────────────────────────────────────────────────────

MONITOR_UPDATABLE = {"title", "event_id", "event_name", "streams", "enabled", "windows", "follow_schedule"}


def _monitor_row(row: sqlite3.Row) -> Dict[str, Any]:
    monitor = dict(row)
    monitor["streams"] = json.loads(monitor.pop("streams_json") or "[]")
    monitor["windows"] = json.loads(monitor.pop("windows_json") or "[]")
    monitor["enabled"] = bool(monitor["enabled"])
    monitor["follow_schedule"] = bool(monitor.get("follow_schedule"))
    return monitor


def _monitor_columns(fields: Dict[str, Any]) -> Dict[str, Any]:
    columns = {}
    for key, value in fields.items():
        if key not in MONITOR_UPDATABLE:
            raise ValueError(f"不能修改的字段：{key}")
        if key in ("streams", "windows"):
            columns[f"{key}_json"] = json.dumps(value or [], ensure_ascii=False)
        elif key in ("enabled", "follow_schedule"):
            columns[key] = int(bool(value))
        else:
            columns[key] = value
    return columns


def create_monitor(**fields: Any) -> Dict[str, Any]:
    now = time.time()
    columns = {"created_at": now, "updated_at": now, **_monitor_columns(fields)}
    with _lock:
        cur = _db().execute(
            f"INSERT INTO monitors ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()),
        )
        _db().commit()
        monitor_id = cur.lastrowid
    return get_monitor(monitor_id)


def get_monitor(monitor_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _db().execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    return _monitor_row(row) if row else None


def list_monitors() -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
    return [_monitor_row(r) for r in rows]


def update_monitor(monitor_id: int, **fields: Any) -> Dict[str, Any]:
    columns = {**_monitor_columns(fields), "updated_at": time.time()}
    with _lock:
        _db().execute(
            f"UPDATE monitors SET {', '.join(f'{k} = ?' for k in columns)} WHERE id = ?",
            list(columns.values()) + [monitor_id],
        )
        _db().commit()
    return get_monitor(monitor_id)


def delete_monitor(monitor_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM monitor_events WHERE monitor_id = ?", (monitor_id,))
        _db().execute("DELETE FROM monitor_cards WHERE monitor_id = ?", (monitor_id,))
        _db().execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        _db().commit()


def add_monitor_event(monitor_id: int, message: str, level: str = "info") -> None:
    with _lock:
        _db().execute(
            "INSERT INTO monitor_events (monitor_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (monitor_id, time.time(), level, message),
        )
        _db().commit()


def recent_monitor_events(monitor_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM monitor_events WHERE monitor_id = ? ORDER BY id DESC LIMIT ?", (monitor_id, limit)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_monitor_card(monitor_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _db().execute("SELECT * FROM monitor_cards WHERE monitor_id = ?", (monitor_id,)).fetchone()
    return dict(row) if row else None


def save_monitor_card(monitor_id: int, **fields: Any) -> None:
    """登记或更新直播间卡片（message_id / sent_at / digest）。"""
    columns = {"monitor_id": monitor_id, **fields}
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields) or "monitor_id = monitor_id"
    with _lock:
        _db().execute(
            f"INSERT INTO monitor_cards ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(monitor_id) DO UPDATE SET {updates}", list(columns.values()))
        _db().commit()


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
