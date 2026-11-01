"""百分位综合分与 Top 100。

同一周期、同一合格集合内比较：Score = 0.5 * P(G) + 0.5 * P(A)，总 Star 不进入综合分。
百分位按平均名次换算，最小 0、最大 100。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db
from .metrics import PERIOD_NAMES, WINDOWS

WINDOW_BY_PERIOD = {period: window for window, period in PERIOD_NAMES.items()}

POOL_SQL = """
SELECT r.repo_id, r.full_name, r.description, r.url, r.stars, r.created_at, r.archived, m.pool,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'domain') AS domains,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'form') AS forms,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'kind') AS kinds
FROM repositories r
JOIN pool_memberships m ON m.repo_id = r.repo_id AND m.valid_to IS NULL
ORDER BY r.repo_id
"""


def percentile(values: list[float]) -> list[float]:
    """并列取平均名次：(平均名次 - 1) / (n - 1) * 100。"""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [100.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    start = 0
    while start < n:
        end = start
        while end + 1 < n and values[order[end + 1]] == values[order[start]]:
            end += 1
        average = (start + end) / 2 + 1
        for index in range(start, end + 1):
            ranks[order[index]] = 100.0 * (average - 1) / (n - 1)
        start = end + 1
    return ranks


def pool_rows(con: sqlite3.Connection, end_day: str) -> tuple[list[dict], dict]:
    """全池每个 (仓库, 周期) 一行；榜单和页面重排都从这里取数。"""
    repos = {row["repo_id"]: dict(row) for row in con.execute(POOL_SQL)}
    metrics = {}
    for row in con.execute("SELECT * FROM metric_windows WHERE end_day = ?", (end_day,)):
        metrics[(row["repo_id"], row["window"])] = dict(row)
    rows = []
    for repo_id, repo in repos.items():
        for window in WINDOWS:
            metric = metrics.get((repo_id, window))
            row = dict(repo)
            row.update({
                "window": window,
                "added": metric["added_current"] if metric else None,
                "added_previous": metric["added_previous"] if metric else None,
                "velocity": metric["velocity"] if metric else None,
                "acceleration": metric["acceleration"] if metric else None,
                "complete": metric["complete"] if metric else 0,
                "days_available": metric["days_available"] if metric else 0,
            })
            rows.append(row)
    return rows, repos


def score_window(rows: list[dict], window: int) -> list[dict]:
    """该周期的全部行，给指标齐全的行补上百分位与综合分（原地写回）。"""
    current = [row for row in rows if row["window"] == window]
    scored = [row for row in current if row["added"] is not None and row["acceleration"] is not None]
    p_added = percentile([row["added"] for row in scored])
    p_accel = percentile([row["acceleration"] for row in scored])
    for row, pa, pb in zip(scored, p_added, p_accel):
        row["p_added"] = round(pa, 2)
        row["p_accel"] = round(pb, 2)
        row["score"] = round(0.5 * pa + 0.5 * pb, 2)
    return current


def newcomer_ids(repos: dict, end_day: str, days: int) -> set[int]:
    """新锐池里在 days 天内创建的仓库；没有创建时间的不排除。"""
    cutoff = (date.fromisoformat(end_day) - timedelta(days=days)).isoformat()
    return {repo_id for repo_id, repo in repos.items()
            if repo["pool"] == "newcomer" and not ((repo["created_at"] or "")[:10] < cutoff)}


def _top(rows: list[dict], key, limit: int) -> list[dict]:
    """返回副本，避免同一仓库在不同榜里共用 dict 而被后算的榜覆盖名次。"""
    ordered = sorted(rows, key=key)[:limit]
    return [dict(row, rank=position) for position, row in enumerate(ordered, start=1)]


def build(con: sqlite3.Connection, cfg: dict, end_day: str) -> dict:
    limit = int(cfg["rank"]["top_n"])
    rows, repos = pool_rows(con, end_day)
    boards: dict[str, list[dict]] = {}
    eligible: dict[str, int] = {}

    for window, period in PERIOD_NAMES.items():
        current = score_window(rows, window)

        growth = [row for row in current if row["added"] is not None]
        eligible[f"{period}_growth"] = len(growth)
        boards[f"{period}_growth"] = _top(
            growth, lambda row: (-row["added"], -(row["acceleration"] or 0), row["repo_id"]), limit)

        accel = [row for row in current if row["acceleration"] is not None]
        eligible[f"{period}_acceleration"] = len(accel)
        boards[f"{period}_acceleration"] = _top(
            accel, lambda row: (-row["acceleration"], -(row["added"] or 0), row["repo_id"]), limit)

        scored = [row for row in current if row.get("score") is not None]
        eligible[f"{period}_score"] = len(scored)
        boards[f"{period}_score"] = _top(
            scored, lambda row: (-row["score"], -row["added"], -row["acceleration"], row["repo_id"]), limit)

    newcomer_days = int(cfg["rank"]["newcomer_days"])
    recent = newcomer_ids(repos, end_day, newcomer_days)
    newcomers = [row for row in rows
                 if row["repo_id"] in recent and row["window"] == 7 and row["added"] is not None]
    eligible["newcomers"] = len(newcomers)
    boards["newcomers"] = _top(
        newcomers, lambda row: (-row["added"], -(row["acceleration"] or 0), row["repo_id"]), limit)

    persist(con, end_day, boards)
    return {"end_day": end_day, "pool_size": len(repos), "boards": boards, "eligible": eligible}


def split_board(name: str) -> tuple[int, str]:
    if name == "newcomers":
        return WINDOW_BY_PERIOD["weekly"], name
    period, board = name.split("_", 1)
    return WINDOW_BY_PERIOD[period], board


def persist(con: sqlite3.Connection, end_day: str, boards: dict[str, list[dict]]) -> None:
    for name, rows in boards.items():
        window, board = split_board(name)
        con.execute("DELETE FROM ranking_snapshots WHERE window = ? AND board = ? AND end_day = ?",
                    (window, board, end_day))
        con.executemany(
            """
            INSERT INTO ranking_snapshots (window, board, end_day, repo_id, added, velocity,
                                           acceleration, p_added, p_accel, score, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(window, board, end_day, row["repo_id"], row.get("added"), row.get("velocity"),
              row.get("acceleration"), row.get("p_added"), row.get("p_accel"), row.get("score"),
              row["rank"]) for row in rows],
        )
