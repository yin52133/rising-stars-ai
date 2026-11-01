"""候选发现：GitHub Search API 分段查询，按仓库 ID 去重并留下来源证据。"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import date, timedelta

from . import db
from .api import API, ApiError, repo_from_api

SEARCH_URL = f"{API}/search/repositories"
RESULT_CAP = 1000  # Search API 单查询上限，超过部分拿不到


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
