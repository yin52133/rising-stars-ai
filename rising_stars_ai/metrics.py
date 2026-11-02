"""窗口指标：新增 G、速度 V、加速度 A 与完整性。

历史要求 2 天 / 14 天 / 60 天；覆盖不足时保留缺测（None），不按 0 计入。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db

WINDOWS = (1, 7, 30)
PERIOD_NAMES = {1: "daily", 7: "weekly", 30: "monthly"}


def resolve_end_day(con: sqlite3.Connection) -> date | None:
    rows = con.execute(
        "SELECT day, COUNT(DISTINCT repo_id) AS n FROM star_history_days "
        "GROUP BY day ORDER BY day DESC LIMIT 2").fetchall()
    if not rows:
        return None
    # shortcut: 新的一天刚开始时官方几乎没有数据，退一天用完整的一天；缺口超过一天要换策略
    if len(rows) > 1 and rows[0]["n"] * 2 < rows[1]["n"]:
        return date.fromisoformat(rows[1]["day"])
    return date.fromisoformat(rows[0]["day"])


def _days(end_day: date, count: int) -> list[str]:
    """count 天，索引 0 是 end_day（最近一天），向后回溯。"""
    return [(end_day - timedelta(days=offset)).isoformat() for offset in range(count)]


def compute(con: sqlite3.Connection, repo_id: int, window: int, end_day: date,
            computed_at: str | None = None) -> dict:
    days = _days(end_day, 2 * window)
    rows = con.execute(
        "SELECT day, added FROM star_history_days WHERE repo_id = ? AND day BETWEEN ? AND ?",
        (repo_id, days[-1], days[0]),
    ).fetchall()
    present = {row["day"]: row["added"] for row in rows}
    current_days, previous_days = days[:window], days[window:]

    added_current = sum(present[d] for d in current_days) if all(d in present for d in current_days) else None
    added_previous = sum(present[d] for d in previous_days) if all(d in present for d in previous_days) else None
    velocity = added_current / window if added_current is not None else None
    acceleration = None
    if velocity is not None and added_previous is not None:
        acceleration = (velocity - added_previous / window) / window

    metric = {
        "repo_id": repo_id,
        "window": window,
        "end_day": end_day.isoformat(),
        "added_current": added_current,
        "added_previous": added_previous,
        "velocity": velocity,
        "acceleration": acceleration,
        "complete": int(added_current is not None and added_previous is not None),
        "days_available": len(present),
    }
    con.execute(
        """
        INSERT INTO metric_windows (repo_id, window, end_day, added_current, added_previous, velocity,
                                    acceleration, complete, days_available, computed_at)
        VALUES (:repo_id, :window, :end_day, :added_current, :added_previous, :velocity,
                :acceleration, :complete, :days_available, :computed_at)
        ON CONFLICT(repo_id, window, end_day) DO UPDATE SET
            added_current = excluded.added_current, added_previous = excluded.added_previous,
            velocity = excluded.velocity, acceleration = excluded.acceleration,
            complete = excluded.complete, days_available = excluded.days_available,
            computed_at = excluded.computed_at
        """,
        {**metric, "computed_at": computed_at or db.now_iso()},
    )
    return metric


def compute_all(con: sqlite3.Connection, end_day: date, computed_at: str | None = None) -> dict:
    repo_ids = [row["repo_id"] for row in con.execute(
        "SELECT repo_id FROM pool_memberships WHERE valid_to IS NULL ORDER BY repo_id")]
    for repo_id in repo_ids:
        for window in WINDOWS:
            compute(con, repo_id, window, end_day, computed_at)
    return {"repos": len(repo_ids), "windows": len(WINDOWS), "rows": len(repo_ids) * len(WINDOWS)}
