"""SQLite 持久化：schema 与幂等写入。

不加外键：批量 upsert 性能优先，去重靠主键与代码侧的来源键。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    repo_id       INTEGER PRIMARY KEY,
    full_name     TEXT    NOT NULL,
    description   TEXT,
    url           TEXT,
    topics        TEXT    NOT NULL DEFAULT '[]',
    stars         INTEGER,
    created_at    TEXT,
    pushed_at     TEXT,
    archived      INTEGER NOT NULL DEFAULT 0,
    is_fork       INTEGER NOT NULL DEFAULT 0,
    is_template   INTEGER NOT NULL DEFAULT 0,
    ai_included   INTEGER,
    ai_confidence REAL,
    checked_at    TEXT,
    first_seen    TEXT    NOT NULL,
    last_seen     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_sources (
    repo_id    INTEGER NOT NULL,
    source     TEXT    NOT NULL,
    source_key TEXT    NOT NULL,
    url        TEXT,
    evidence   TEXT,
    first_seen TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL,
    PRIMARY KEY (repo_id, source, source_key)
);

CREATE TABLE IF NOT EXISTS repository_labels (
    repo_id    INTEGER NOT NULL,
    dimension  TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    evidence   TEXT,
    method     TEXT,
    confidence REAL,
    PRIMARY KEY (repo_id, dimension, label)
);

CREATE TABLE IF NOT EXISTS pool_memberships (
    repo_id    INTEGER NOT NULL,
    valid_from TEXT    NOT NULL,
    valid_to   TEXT,
    pool       TEXT    NOT NULL,
    reason     TEXT,
    PRIMARY KEY (repo_id, valid_from)
);

CREATE TABLE IF NOT EXISTS star_history_days (
    repo_id      INTEGER NOT NULL,
    day          TEXT    NOT NULL,
    added        INTEGER NOT NULL,
    week         TEXT,
    source       TEXT,
    page         INTEGER,
    collected_at TEXT,
    run_id       TEXT,
    PRIMARY KEY (repo_id, day)
);

CREATE TABLE IF NOT EXISTS star_total_snapshots (
    repo_id      INTEGER NOT NULL,
    captured_at  TEXT    NOT NULL,
    stars        INTEGER NOT NULL,
    run_id       TEXT,
    PRIMARY KEY (repo_id, captured_at)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    requests    INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    data_until  TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS metric_windows (
    repo_id        INTEGER NOT NULL,
    window         INTEGER NOT NULL,
    end_day        TEXT    NOT NULL,
    added_current  INTEGER,
    added_previous INTEGER,
    velocity       REAL,
    acceleration   REAL,
    complete       INTEGER NOT NULL DEFAULT 0,
    days_available INTEGER NOT NULL DEFAULT 0,
    computed_at    TEXT,
    PRIMARY KEY (repo_id, window, end_day)
);

CREATE TABLE IF NOT EXISTS ranking_snapshots (
    window       INTEGER NOT NULL,
    board        TEXT    NOT NULL,
    end_day      TEXT    NOT NULL,
    repo_id      INTEGER NOT NULL,
    added        INTEGER,
    velocity     REAL,
    acceleration REAL,
    p_added      REAL,
    p_accel      REAL,
    score        REAL,
    rank         INTEGER NOT NULL,
    PRIMARY KEY (window, board, end_day, repo_id)
);

CREATE INDEX IF NOT EXISTS idx_star_days_repo ON star_history_days (repo_id, day);
CREATE INDEX IF NOT EXISTS idx_memberships_open ON pool_memberships (repo_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_metrics_end ON metric_windows (window, end_day);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    db_path = pathlib.Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def topics_of(row: sqlite3.Row | dict) -> list[str]:
    raw = row["topics"] if not isinstance(row, dict) else row.get("topics")
    return json.loads(raw) if raw else []


def repo_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["topics"] = json.loads(data.get("topics") or "[]")
    return data


def upsert_repository(con: sqlite3.Connection, repo: dict, seen_at: str) -> None:
    """缺失字段沿用旧值；未知的 Star 数不覆盖为 0。"""
    params = {
        "repo_id": repo["repo_id"],
        "full_name": repo["full_name"],
        "description": repo.get("description"),
        "url": repo.get("url"),
        "topics": json.dumps(repo.get("topics") or [], ensure_ascii=False),
        "stars": repo.get("stars"),
        "created_at": repo.get("created_at"),
        "pushed_at": repo.get("pushed_at"),
        "archived": int(bool(repo.get("archived"))),
        "is_fork": int(bool(repo.get("is_fork"))),
        "is_template": int(bool(repo.get("is_template"))),
        "seen_at": seen_at,
    }
    con.execute(
        """
        INSERT INTO repositories (repo_id, full_name, description, url, topics, stars, created_at,
                                  pushed_at, archived, is_fork, is_template, first_seen, last_seen)
        VALUES (:repo_id, :full_name, :description, :url, :topics, :stars, :created_at,
                :pushed_at, :archived, :is_fork, :is_template, :seen_at, :seen_at)
        ON CONFLICT(repo_id) DO UPDATE SET
            full_name   = excluded.full_name,
            description = COALESCE(excluded.description, repositories.description),
            url         = COALESCE(excluded.url, repositories.url),
            topics      = CASE WHEN excluded.topics = '[]' THEN repositories.topics ELSE excluded.topics END,
            stars       = COALESCE(excluded.stars, repositories.stars),
            created_at  = COALESCE(excluded.created_at, repositories.created_at),
            pushed_at   = COALESCE(excluded.pushed_at, repositories.pushed_at),
            archived    = excluded.archived,
            is_fork     = excluded.is_fork,
            is_template = excluded.is_template,
            last_seen   = excluded.last_seen
        """,
        params,
    )


def add_source(con: sqlite3.Connection, repo_id: int, source: str, source_key: str,
               url: str | None, evidence: str | None, seen_at: str) -> None:
    con.execute(
        """
        INSERT INTO discovery_sources (repo_id, source, source_key, url, evidence, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, source, source_key) DO UPDATE SET
            url       = COALESCE(excluded.url, discovery_sources.url),
            evidence  = COALESCE(excluded.evidence, discovery_sources.evidence),
            last_seen = excluded.last_seen
        """,
        (repo_id, source, source_key, url, evidence, seen_at, seen_at),
    )


def set_labels(con: sqlite3.Connection, repo_id: int, rows: list[tuple], method: str) -> None:
    """rows: (dimension, label, evidence, confidence)。整仓替换，避免旧标签残留。"""
    con.execute("DELETE FROM repository_labels WHERE repo_id = ?", (repo_id,))
    con.executemany(
        "INSERT OR REPLACE INTO repository_labels (repo_id, dimension, label, evidence, method, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(repo_id, dim, label, evidence, method, confidence) for dim, label, evidence, confidence in rows],
    )


def set_classification(con: sqlite3.Connection, repo_id: int, included: bool,
                       confidence: float, checked_at: str) -> None:
    con.execute(
        "UPDATE repositories SET ai_included = ?, ai_confidence = ?, checked_at = ? WHERE repo_id = ?",
        (int(bool(included)), confidence, checked_at, repo_id),
    )


def open_memberships(con: sqlite3.Connection) -> dict[int, dict]:
    rows = con.execute(
        "SELECT repo_id, pool, valid_from FROM pool_memberships WHERE valid_to IS NULL"
    ).fetchall()
    return {row["repo_id"]: dict(row) for row in rows}


def sync_memberships(con: sqlite3.Connection, desired: dict[int, tuple[str, str]],
                     changed_at: str, close_note: str = "未进入本周池") -> dict:
    """desired: repo_id -> (pool, reason)。池或身份变化时关闭旧记录并新开一条，保留历史与原因。"""
    current = open_memberships(con)
    promoted = 0
    demoted = 0
    for repo_id, row in current.items():
        want = desired.get(repo_id)
        if want and want[0] == row["pool"]:
            continue
        note = f" | 晋级: {want[0]}" if want else f" | 移出: {close_note}"
        con.execute(
            "UPDATE pool_memberships SET valid_to = ?, reason = COALESCE(reason, '') || ?"
            " WHERE repo_id = ? AND valid_from = ?",
            (changed_at, note, repo_id, row["valid_from"]),
        )
        if want:
            promoted += 1
        else:
            demoted += 1
    added = 0
    for repo_id, (pool, reason) in desired.items():
        row = current.get(repo_id)
        if row and row["pool"] == pool:
            continue
        con.execute(
            "INSERT OR REPLACE INTO pool_memberships (repo_id, valid_from, valid_to, pool, reason)"
            " VALUES (?, ?, NULL, ?, ?)",
            (repo_id, changed_at, pool, reason),
        )
        added += 1
    return {"added": added, "promoted": promoted, "demoted": demoted}


def upsert_star_days(con: sqlite3.Connection, repo_id: int, rows: list[dict],
                     source: str, run_id: str, collected_at: str, page: int | None = None) -> int:
    con.executemany(
        """
        INSERT INTO star_history_days (repo_id, day, added, week, source, page, collected_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, day) DO UPDATE SET
            added = excluded.added, week = excluded.week, collected_at = excluded.collected_at,
            run_id = excluded.run_id, source = excluded.source, page = excluded.page
        """,
        [(repo_id, row["day"], row["added"], row.get("week"), source, page, collected_at, run_id)
         for row in rows],
    )
    return len(rows)


def add_total_snapshot(con: sqlite3.Connection, repo_id: int, stars: int,
                       captured_at: str, run_id: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO star_total_snapshots (repo_id, captured_at, stars, run_id)"
        " VALUES (?, ?, ?, ?)",
        (repo_id, captured_at, stars, run_id),
    )


def start_run(con: sqlite3.Connection, kind: str, started_at: str | None = None) -> str:
    run_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    con.execute(
        "INSERT INTO collection_runs (run_id, kind, started_at, status) VALUES (?, ?, ?, 'running')",
        (run_id, kind, started_at or now_iso()),
    )
    return run_id


def finish_run(con: sqlite3.Connection, run_id: str, status: str, requests: int,
               errors: int, data_until: str | None = None, note: str | None = None) -> None:
    con.execute(
        "UPDATE collection_runs SET finished_at = ?, status = ?, requests = ?, errors = ?,"
        " data_until = ?, note = ? WHERE run_id = ?",
        (now_iso(), status, requests, errors, data_until, note, run_id),
    )
