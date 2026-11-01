"""候选发现：Search API、Trending 与可选 OSSInsight，按仓库 ID 去重并留下来源证据。"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import date, timedelta

from . import db
from .api import API, REPO_URL, ApiError, repo_from_api

SEARCH_URL = f"{API}/search/repositories"
RESULT_CAP = 1000  # Search API 单查询上限，超过部分拿不到
TRENDING_REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="/([\w.-]+/[\w.-]+)"')


def build_queries(cfg: dict, today: date) -> list[dict]:
    """按关键词、创建时间与 Star 区间拆分查询，不靠单次宽查询假装全站覆盖。"""
    search = cfg["github_search"]
    pushed = (today - timedelta(days=search["pushed_within_days"])).isoformat()
    queries = []
    for topic in search["topics"]:
        for bucket in search["star_buckets"]:
            queries.append({
                "key": f"topic:{topic}|stars:{bucket}",
                "q": f"topic:{topic} stars:{bucket} pushed:>={pushed} archived:false",
            })
    for keyword in search["name_keywords"]:
        for created in search["created_buckets"]:
            queries.append({
                "key": f"kw:{keyword}|created:{created}",
                "q": (f'"{keyword}" in:name,description,readme created:{created} '
                      f"stars:>={search['low_star_floor']} archived:false"),
            })
    return queries


def query_url(q: str) -> str:
    return "https://github.com/search?" + urllib.parse.urlencode({"q": q, "type": "repositories"})


def run_search(con: sqlite3.Connection, client, cfg: dict, run_id: str,
               seen_at: str, max_queries: int | None = None) -> dict:
    search = cfg["github_search"]
    queries = build_queries(cfg, date.fromisoformat(seen_at[:10]))
    if max_queries:
        queries = queries[:max_queries]

    found: dict[int, str] = {}
    truncated = 0
    errors = 0
    for query in queries:
        for page in range(1, search["max_pages"] + 1):
            params = {"q": query["q"], "per_page": search["per_page"], "page": page,
                      "sort": "stars", "order": "desc"}
            try:
                payload = client.get_json(SEARCH_URL, params)
            except ApiError:
                errors += 1
                break
            items = payload.get("items") or []
            total = int(payload.get("total_count") or 0)
            if page == 1 and total > RESULT_CAP:
                truncated += 1
            for item in items:
                repo = repo_from_api(item)
                db.upsert_repository(con, repo, seen_at)
                db.add_source(con, repo["repo_id"], "github_search", query["key"],
                              query_url(query["q"]),
                              json.dumps({"query": query["q"], "page": page, "total_count": total},
                                         ensure_ascii=False),
                              seen_at)
                found[repo["repo_id"]] = repo["full_name"]
            if len(items) < search["per_page"]:
                break
    return {"queries": len(queries), "repos": len(found), "truncated_queries": truncated,
            "errors": errors, "requests": client.requests}


def parse_trending(html: str) -> list[str]:
    """Trending 页面只给 owner/repo，去重保序。"""
    return list(dict.fromkeys(TRENDING_REPO_RE.findall(html)))


def _store_by_name(con: sqlite3.Connection, client, source: str, evidence: dict,
                   full_names: list[str], seen_at: str) -> tuple[dict[int, str], int]:
    """外部来源只提供名字，元数据走 Repository API；拿不到就记错误，不造仓库。"""
    found: dict[int, str] = {}
    errors = 0
    for full_name in full_names:
        try:
            meta = client.get_json(REPO_URL.format(full_name=full_name))
        except ApiError:
            errors += 1
            continue
        repo = repo_from_api(meta)
        db.upsert_repository(con, repo, seen_at)
        db.add_source(con, repo["repo_id"], source, f"{evidence['key']}:{full_name}",
                      f"https://github.com/{full_name}",
                      json.dumps(evidence, ensure_ascii=False), seen_at)
        found[repo["repo_id"]] = repo["full_name"]
    return found, errors


def run_trending(con: sqlite3.Connection, client, cfg: dict, run_id: str, seen_at: str) -> dict:
    trending = cfg["github_trending"]
    if not trending.get("enabled"):
        return {"enabled": False}
    found: dict[int, str] = {}
    errors = 0
    for since in trending["since"]:
        try:
            html = client.get_text(trending["url"].format(since=since), accept="text/html")
        except ApiError:
            errors += 1
            continue
        names = parse_trending(html)[: trending["max_items"]]
        stored, failed = _store_by_name(
            con, client, "github_trending",
            {"key": f"trending:{since}", "since": since, "items": len(names),
             "url": trending["url"].format(since=since)},
            names, seen_at)
        found.update(stored)
        errors += failed
    return {"enabled": True, "repos": len(found), "errors": errors, "requests": client.requests}


def run_ossinsight(con: sqlite3.Connection, client, cfg: dict, run_id: str, seen_at: str) -> dict:
    """可选候选来源：只补充发现，不导入其榜单名次，也不参与 Star 计算。"""
    ossinsight = cfg["ossinsight"]
    if not ossinsight.get("enabled"):
        return {"enabled": False}
    try:
        payload = client.get_json(ossinsight["url"], {"period": ossinsight["period"]})
    except ApiError:
        return {"enabled": True, "repos": 0, "errors": 1}
    rows = (payload.get("data") or {}).get("rows") or []
    names = [row.get("repo_name") for row in rows if row.get("repo_name")][: ossinsight["max_items"]]
    found, errors = _store_by_name(
        con, client, "ossinsight",
        {"key": f"ossinsight:{ossinsight['period']}", "period": ossinsight["period"],
         "items": len(names), "url": ossinsight["url"]},
        names, seen_at)
    return {"enabled": True, "repos": len(found), "errors": errors, "requests": client.requests}
