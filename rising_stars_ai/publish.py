"""发布：每榜一份 JSON、一份全量 CSV 与静态页面。原始指标全部保留。"""

from __future__ import annotations

import csv
import datetime
import json
import pathlib
import sqlite3

from .metrics import PERIOD_NAMES

BOARD_LABELS = {
    "growth": "绝对增长",
    "acceleration": "加速度",
    "score": "综合分",
    "newcomers": "新锐",
}

BOARD_SQL = """
SELECT s.rank, s.repo_id, s.added, s.velocity, s.acceleration, s.p_added, s.p_accel, s.score,
       r.full_name, r.description, r.url, r.stars, r.created_at, m.pool,
       w.complete, w.added_previous, w.days_available,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'domain') AS domains,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'form') AS forms,
       (SELECT group_concat(label) FROM repository_labels l
         WHERE l.repo_id = r.repo_id AND l.dimension = 'kind') AS kinds
FROM ranking_snapshots s
JOIN repositories r ON r.repo_id = s.repo_id
LEFT JOIN pool_memberships m ON m.repo_id = s.repo_id AND m.valid_to IS NULL
LEFT JOIN metric_windows w ON w.repo_id = s.repo_id AND w.window = s.window AND w.end_day = s.end_day
WHERE s.end_day = ? AND s.window = ? AND s.board = ?
ORDER BY s.rank
"""


def _labels(value: str | None) -> list[str]:
    return [item for item in (value or "").split(",") if item]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def pool_meta(con: sqlite3.Connection) -> dict:
    counts = {row["pool"]: row["n"] for row in con.execute(
        "SELECT pool, COUNT(*) AS n FROM pool_memberships WHERE valid_to IS NULL GROUP BY pool")}
    no_history = con.execute(
        "SELECT COUNT(*) FROM pool_memberships m WHERE m.valid_to IS NULL AND NOT EXISTS"
        " (SELECT 1 FROM star_history_days h WHERE h.repo_id = m.repo_id)").fetchone()[0]
    return {
        "repos": sum(counts.values()),
        "regular": counts.get("regular", 0),
        "newcomer": counts.get("newcomer", 0),
        "no_history": no_history,
        "scope": "Top 100 是当前监控池内排名，不宣称全站无遗漏",
        "window_note": "指标来自官方 Star 历史接口的新增 Star；官方日/周边界不保证按 UTC 对齐",
    }


def board_names(con: sqlite3.Connection, end_day: str) -> list[tuple[str, int, str]]:
    """出榜当天固定输出 3 周期 × 3 榜 + 新锐榜；空榜也落一份空文件，方便下游稳定读取。"""
    windows = {row["window"] for row in con.execute(
        "SELECT DISTINCT window FROM ranking_snapshots WHERE end_day = ?", (end_day,))}
    if not windows:
        return []
    names = [(f"{PERIOD_NAMES[window]}_{board}", window, board)
             for window in (1, 7, 30) if window in windows
             for board in ("growth", "acceleration", "score")]
    names.append(("newcomers", 7, "newcomers"))
    return names


def _items(con: sqlite3.Connection, window: int, board: str, end_day: str) -> list[dict]:
    items = []
    for row in con.execute(BOARD_SQL, (end_day, window, board)):
        items.append({
            "rank": row["rank"],
            "repo_id": row["repo_id"],
            "full_name": row["full_name"],
            "url": row["url"],
            "description": row["description"],
            "stars": row["stars"],
            "created_at": row["created_at"],
            "pool": row["pool"],
            "domains": _labels(row["domains"]),
            "forms": _labels(row["forms"]),
            "kinds": _labels(row["kinds"]),
            "added": row["added"],
            "added_previous": row["added_previous"],
            "velocity": row["velocity"],
            "acceleration": row["acceleration"],
            "p_added": row["p_added"],
            "p_accel": row["p_accel"],
            "score": row["score"],
            "complete": bool(row["complete"]),
            "days_available": row["days_available"],
        })
    return items


def publish(con: sqlite3.Connection, cfg: dict, end_day: str,
            out_dir: str | pathlib.Path | None = None, generated_at: str | None = None) -> dict:
    target = pathlib.Path(out_dir or cfg["publish"]["out_dir"])
    target.mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or _now()
    pool = pool_meta(con)

    boards: dict[str, dict] = {}
    for name, window, board in board_names(con, end_day):
        items = _items(con, window, board, end_day)
        boards[name] = {
            "board": name,
            "period": "weekly" if board == "newcomers" else name.split("_")[0],
            "window": window,
            "board_type": board,
            "board_label": BOARD_LABELS.get(board, board),
            "end_day": end_day,
            "generated_at": generated_at,
            "pool": pool,
            "incomplete_items": sum(1 for item in items if not item["complete"]),
            "items": items,
        }

    files = []
    for name, payload in boards.items():
        path = target / f"{name}_top100.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(path)

    csv_path = target / f"rankings_{end_day}.csv"
    _write_csv(csv_path, boards)
    files.append(csv_path)

    meta_path = target / "meta.json"
    meta_path.write_text(json.dumps(
        {"end_day": end_day, "generated_at": generated_at, "pool": pool,
         "boards": {name: len(payload["items"]) for name, payload in boards.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    files.append(meta_path)

    page = target / "index.html"
    page.write_text(render_page(boards, pool, end_day, generated_at), encoding="utf-8")
    files.append(page)

    return {
        "end_day": end_day,
        "boards": {name: len(payload["items"]) for name, payload in boards.items()},
        "files": [str(path) for path in files],
    }


def _write_csv(path: pathlib.Path, boards: dict[str, dict]) -> None:
    numeric = ("rank", "repo_id", "full_name", "url", "description", "pool", "stars", "created_at",
               "added", "added_previous", "velocity", "acceleration", "p_added", "p_accel",
               "score", "complete", "days_available")
    fields = ["board", *numeric, "domains", "forms", "kinds"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for name, payload in boards.items():
            for item in payload["items"]:
                row = {key: item[key] for key in numeric}
                row["board"] = name
                for key in ("domains", "forms", "kinds"):
                    row[key] = "|".join(item[key])
                if row["description"]:
                    row["description"] = " ".join(row["description"].split())
                writer.writerow(row)




def render_page(boards: dict[str, dict], pool: dict, end_day: str, generated_at: str) -> str:
    directory = pathlib.Path(__file__).parent
    descriptions = json.loads((directory / "repository_descriptions.json").read_text(encoding="utf-8"))
    repo_ids = {str(item["repo_id"]) for board in boards.values() for item in board["items"]}
    payload = json.dumps({
        "meta": {"pool": pool, "generated_at": generated_at},
        "end_day": end_day,
        "boards": boards,
        "descriptions": {repo_id: descriptions[repo_id] for repo_id in repo_ids if repo_id in descriptions},
    }, ensure_ascii=False).replace("</", "<\\/")
    return (directory / "rankings.html").read_text(encoding="utf-8").replace("__PAYLOAD__", payload)
