"""监控池：粗筛、领域基础名额与机动名额、晋级与降级留痕。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db

CANDIDATE_SQL = """
SELECT r.repo_id, r.full_name, r.stars, r.created_at, r.pushed_at,
       r.archived, r.is_fork, r.is_template,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'domain') AS domains,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'kind') AS kinds,
       (SELECT COUNT(DISTINCT source) FROM discovery_sources s WHERE s.repo_id = r.repo_id) AS sources
FROM repositories r
WHERE r.ai_included = 1
"""


def recent_growth(con: sqlite3.Connection, ref_day: date, window: int = 7) -> dict[int, int]:
    """只统计覆盖满窗口的仓库；缺测的日子让该仓库退出增长排序，不按 0 计。"""
    start = (ref_day - timedelta(days=window - 1)).isoformat()
    rows = con.execute(
        "SELECT repo_id, SUM(added) AS added, COUNT(*) AS days FROM star_history_days"
        " WHERE day BETWEEN ? AND ? GROUP BY repo_id",
        (start, ref_day.isoformat()),
    ).fetchall()
    return {row["repo_id"]: row["added"] for row in rows if row["days"] == window}


def _days_since(value: str | None, ref_day: date) -> int | None:
    if not value:
        return None
    try:
        return (ref_day - date.fromisoformat(value[:10])).days
    except ValueError:
        return None


def bucket(row: sqlite3.Row, pool: dict, growth: dict[int, int], ref_day: date) -> str | None:
    """返回 regular / newcomer / None（粗筛不通过）。"""
    stars = row["stars"] or 0
    g7 = growth.get(row["repo_id"], 0)
    kinds = row["kinds"] or ""
    age = _days_since(row["created_at"], ref_day)
    pushed = _days_since(row["pushed_at"], ref_day)

    if row["archived"] or row["is_template"]:
        return None
    if row["is_fork"] and kinds != "replica":
        return None
    if age is not None and age <= pool["newcomer_days"] and stars < pool["newcomer_max_stars"]:
        return "newcomer"
    if stars >= pool["min_stars"]:
        fresh = pushed is not None and pushed <= pool["pushed_within_days"]
        return "regular" if fresh or g7 >= pool["strong_growth_7d"] else None
    if row["sources"] >= 2 or g7 >= pool["newcomer_growth_7d"]:
        return "newcomer"
    return None


def _domains(row: sqlite3.Row) -> list[str]:
    return [d for d in (row["domains"] or "").split(",") if d]


def build_pool(con: sqlite3.Connection, cfg: dict, ref_day: date, changed_at: str,
               dry_run: bool = False) -> dict:
    pool = cfg["pool"]
    growth = recent_growth(con, ref_day)
    rows = con.execute(CANDIDATE_SQL).fetchall()
    by_name = {row["full_name"].lower(): row for row in rows}

    regular: list[sqlite3.Row] = []
    newcomer: list[sqlite3.Row] = []
    rejected = {"resource_list": 0, "coarse": 0}
    for row in rows:
        if (row["kinds"] or "") == "resource_list":
            rejected["resource_list"] += 1
            continue
        target = bucket(row, pool, growth, ref_day)
        if target is None:
            rejected["coarse"] += 1
            continue
        (newcomer if target == "newcomer" else regular).append(row)

    def strength(row: sqlite3.Row) -> tuple:
        return (-growth.get(row["repo_id"], 0), -(row["stars"] or 0), row["repo_id"])

    regular.sort(key=strength)
    newcomer.sort(key=strength)

    # 各领域先占基础名额，剩余名额按近期表现分配，避免单一领域挤满全榜。
    desired: dict[int, tuple[str, str]] = {}
    per_domain: dict[str, list[sqlite3.Row]] = {}
    for row in regular:
        for domain in _domains(row):
            per_domain.setdefault(domain, []).append(row)
    for domain in sorted(per_domain, key=lambda d: (-len(per_domain[d]), d)):
        for row in per_domain[domain][: pool["domain_base_quota"]]:
            if len(desired) >= pool["regular_size"]:
                break
            desired.setdefault(row["repo_id"], ("regular", f"domain:{domain} 基础名额"))
    for row in regular:
        if len(desired) >= pool["regular_size"]:
            break
        desired.setdefault(row["repo_id"], ("regular", f"机动名额 7日新增 {growth.get(row['repo_id'], 0)}"))

    new_desired: dict[int, tuple[str, str]] = {}
    for row in newcomer[: pool["newcomer_size"]]:
        new_desired[row["repo_id"]] = (
            "newcomer",
            f"新锐观察 created={(row['created_at'] or '?')[:10]} 7日新增 {growth.get(row['repo_id'], 0)}",
        )

    # 人工保留/排除：必须带原因，且保留只在本周容量内生效。
    for full_name, reason in (pool.get("manual_keep") or {}).items():
        row = by_name.get(full_name.lower())
        if row is None or len(desired) >= pool["regular_size"]:
            continue
        desired.setdefault(row["repo_id"], ("regular", f"人工保留: {reason}"))
        new_desired.pop(row["repo_id"], None)
    excluded = {by_name[name.lower()]["repo_id"] for name in (pool.get("manual_exclude") or {})
                if name.lower() in by_name}
    if excluded:
        desired = {k: v for k, v in desired.items() if k not in excluded}
        new_desired = {k: v for k, v in new_desired.items() if k not in excluded}

    desired.update(new_desired)
    summary = {
        "candidates": len(rows),
        "regular": sum(1 for pool_name, _ in desired.values() if pool_name == "regular"),
        "newcomer": sum(1 for pool_name, _ in desired.values() if pool_name == "newcomer"),
        "rejected": rejected,
        "excluded_manual": len(excluded),
    }
    if dry_run:
        summary["desired"] = desired
        return summary
    summary["membership"] = db.sync_memberships(con, desired, changed_at)
    return summary
