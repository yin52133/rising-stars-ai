"""官方 Star 历史回填与每日增量：失败记缺测，绝不写零。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db
from .api import REPO_URL, ApiError, repo_from_api


def iso_week(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def parse_history(payload) -> list[dict]:
    """兼容 {"days": [...]} 与直接列表；缺 added 的行按缺测跳过，不当成 0。"""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("days") or payload.get("history") or payload.get("data") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row.get("day") or row.get("date")
        added = row.get("added", row.get("count"))
        if not day or added is None:
            continue
        out.append({"day": str(day)[:10], "added": int(added)})
    return sorted(out, key=lambda item: item["day"])


def fetch_history(client, cfg: dict, full_name: str, since: str | None) -> list[dict]:
    params = {"since": since} if since else None
    url = cfg["stars"]["endpoint"].format(full_name=full_name)
    rows = parse_history(client.get_json(url, params))
    return [row for row in rows if not since or row["day"] > since]


def pool_repos(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT r.* FROM repositories r"
        " JOIN pool_memberships m ON m.repo_id = r.repo_id AND m.valid_to IS NULL"
        " ORDER BY r.stars DESC, r.repo_id"
    ).fetchall()
    return [db.repo_dict(row) for row in rows]


def _store(con, repo_id: int, data: list[dict], run_id: str, collected_at: str) -> int:
    for row in data:
        row["week"] = iso_week(row["day"])
    return db.upsert_star_days(con, repo_id, data, "github_history", run_id, collected_at)


def backfill(con: sqlite3.Connection, client, cfg: dict, run_id: str, collected_at: str,
             limit: int | None = None) -> dict:
    """新入池仓库取最近 90 天；已有历史的仓库跳过，避免重复计数。"""
    today = date.fromisoformat(collected_at[:10])
    since = (today - timedelta(days=cfg["stars"]["backfill_days"])).isoformat()
    known = {row["repo_id"] for row in con.execute(
        "SELECT DISTINCT repo_id FROM star_history_days").fetchall()}
    stats = {"repos": 0, "days": 0, "missing": 0}
    targets = [repo for repo in pool_repos(con) if repo["repo_id"] not in known]
    for repo in targets[:limit] if limit else targets:
        stats["repos"] += 1
        try:
            data = fetch_history(client, cfg, repo["full_name"], since)
        except ApiError:
            stats["missing"] += 1
            continue
        if not data:
            stats["missing"] += 1
            continue
        stats["days"] += _store(con, repo["repo_id"], data, run_id, collected_at)
    stats["requests"] = client.requests
    stats["errors"] = client.errors
    return stats


def update_daily(con: sqlite3.Connection, client, cfg: dict, run_id: str, collected_at: str,
                 limit: int | None = None) -> dict:
    """只补最近未完成周期，并另存总 Star 快照（净变化单独展示，不参与排名）。"""
    today = date.fromisoformat(collected_at[:10])
    floor = today - timedelta(days=cfg["stars"]["backfill_days"])
    stats = {"repos": 0, "days": 0, "missing": 0, "snapshots": 0, "skipped": 0}
    targets = pool_repos(con)
    for repo in targets[:limit] if limit else targets:
        last = con.execute("SELECT MAX(day) FROM star_history_days WHERE repo_id = ?",
                           (repo["repo_id"],)).fetchone()[0]
        since_day = date.fromisoformat(last) if last else floor
        stats["repos"] += 1
        if since_day < today - timedelta(days=1):
            try:
                data = fetch_history(client, cfg, repo["full_name"], since_day.isoformat())
            except ApiError:
                stats["missing"] += 1
                data = []
            stats["days"] += _store(con, repo["repo_id"], data, run_id, collected_at) if data else 0
        else:
            stats["skipped"] += 1
        try:
            meta = client.get_json(REPO_URL.format(full_name=repo["full_name"]))
        except ApiError:
            stats["missing"] += 1
            continue
        db.upsert_repository(con, repo_from_api(meta), collected_at)
        stars = meta.get("stargazers_count")
        if stars is not None:
            db.add_total_snapshot(con, repo["repo_id"], int(stars), collected_at, run_id)
            stats["snapshots"] += 1
    stats["requests"] = client.requests
    stats["errors"] = client.errors
    return stats
