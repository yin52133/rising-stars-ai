"""AI 相关性与多标签分类：规则优先，README 只在准备入池时复核。"""

from __future__ import annotations

import re
import sqlite3

from . import db
from .api import API, ApiError

_MATCHERS: dict[str, re.Pattern] = {}


def _matcher(keyword: str) -> re.Pattern:
    if keyword not in _MATCHERS:
        if re.fullmatch(r"[a-z]+", keyword):
            _MATCHERS[keyword] = re.compile(rf"(?<![a-z0-9]){keyword}(?![a-z0-9])")
        else:
            _MATCHERS[keyword] = re.compile(re.escape(keyword))
    return _MATCHERS[keyword]


def _hits(text: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if _matcher(kw).search(text)]


def searchable_text(repo: dict) -> str:
    topics = " ".join(repo.get("topics") or [])
    return f"{repo.get('full_name') or ''} {repo.get('description') or ''} {topics}".lower()


def classify(repo: dict, tax: dict) -> dict:
    """输出 design.md §5.3 的分类结构；一个仓库可有多个领域与形态标签。"""
    text = searchable_text(repo)
    name = (repo.get("full_name") or "").lower()
    topics = [t.lower() for t in repo.get("topics") or []]

    kind_hits = {kind: _hits(text, keywords) for kind, keywords in tax["kinds"].items()}
    kind = next((k for k in tax["kind_order"] if kind_hits.get(k)), "tool")

    topic_signals = sorted({t for t in topics if any(_matcher(kw).search(t) for kw in tax["ai_keywords"])})
    text_signals = _hits(text, tax["ai_keywords"])
    signals = sorted(set(topic_signals) | set(text_signals))
    included = bool(topic_signals) or len(text_signals) >= tax["min_signals"]

    domains = [name_ for name_, kws in tax["domains"].items() if _hits(text, kws)]
    forms = [name_ for name_, kws in tax["forms"].items() if _hits(text, kws)]

    evidence = [f"topic:{t}" for t in topic_signals] + [f"text:{t}" for t in text_signals[:5]]
    if kind_hits.get(kind):
        evidence.append(f"kind:{kind}<-" + ",".join(kind_hits[kind][:3]))
    if not included:
        evidence.append("no-ai-signal")

    confidence = min(0.95, 0.30 + 0.12 * len(signals) + (0.15 if kind != "tool" else 0.0))
    if not included:
        confidence = min(0.30, 0.05 + 0.05 * len(signals))
    return {
        "included": included,
        "domains": domains,
        "forms": forms,
        "kind": kind,
        "evidence": evidence,
        "confidence": round(confidence, 2),
        "signals": signals,
    }


def store(con: sqlite3.Connection, repo: dict, result: dict, method: str, checked_at: str) -> None:
    rows = [(dimension, label, "; ".join(result["evidence"]), result["confidence"])
            for dimension, labels in (("domain", result["domains"]), ("form", result["forms"]),
                                      ("kind", [result["kind"]]))
            for label in labels]
    db.set_labels(con, repo["repo_id"], rows, method)
    db.set_classification(con, repo["repo_id"], result["included"], result["confidence"], checked_at)


def classify_pending(con: sqlite3.Connection, tax: dict, checked_at: str, limit: int | None = None) -> dict:
    rows = con.execute(
        "SELECT * FROM repositories WHERE ai_included IS NULL ORDER BY stars DESC LIMIT ?",
        (limit if limit else -1,),
    ).fetchall()
    included = 0
    for row in rows:
        repo = db.repo_dict(row)
        result = classify(repo, tax)
        store(con, repo, result, "rule", checked_at)
        included += int(result["included"])
    return {"checked": len(rows), "included": included}


def confirm_readme(con: sqlite3.Connection, client, tax: dict, repo_ids: list[int], checked_at: str) -> dict:
    """入池前用 README 复核：命中更多 AI 证据则提高置信度，全无证据则排除。"""
    reviewed = 0
    rejected = 0
    for repo_id in repo_ids:
        row = con.execute("SELECT * FROM repositories WHERE repo_id = ?", (repo_id,)).fetchone()
        if row is None:
            continue
        repo = db.repo_dict(row)
        try:
            readme = client.get_text(f"{API}/repos/{row['full_name']}/readme")[:20000]
        except ApiError:  # 采集失败只降级为未复核，不改变已有结论
            readme = None
        if readme is None:
            continue
        hits = _hits(readme.lower(), tax["ai_keywords"])
        result = classify(repo, tax)
        if hits:
            result["evidence"].append("readme:" + ",".join(hits[:5]))
            result["confidence"] = round(min(0.95, result["confidence"] + 0.1), 2)
            result["included"] = True
        elif not any(e.startswith("topic:") for e in result["evidence"]):
            result["included"] = False
            result["evidence"].append("readme-no-ai-signal")
            rejected += 1
        store(con, repo, result, "rule+readme", checked_at)
        reviewed += 1
    return {"reviewed": reviewed, "rejected": rejected}
