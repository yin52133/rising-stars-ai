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
    names = []
    for row in con.execute(
            "SELECT DISTINCT window, board FROM ranking_snapshots WHERE end_day = ?"
            " ORDER BY window, board", (end_day,)):
        if row["board"] == "newcomers":
            names.append(("newcomers", row["window"], "newcomers"))
        else:
            names.append((f"{PERIOD_NAMES[row['window']]}_{row['board']}", row["window"], row["board"]))
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


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rising Stars AI · GitHub AI 项目趋势</title>
<style>
:root { color-scheme: light dark; --line: #d9dde3; --muted: #6b7280; --accent: #2563eb; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.55 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; }
header { padding: 24px clamp(16px, 4vw, 48px) 8px; }
h1 { margin: 0 0 4px; font-size: 22px; }
.sub { margin: 0 0 16px; color: var(--muted); }
dl.meta { display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 0; }
dl.meta div { display: flex; gap: 6px; align-items: baseline; }
dt { color: var(--muted); font-size: 12px; }
dd { margin: 0; font-variant-numeric: tabular-nums; }
main { padding: 8px clamp(16px, 4vw, 48px) 40px; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0; }
.controls label { display: flex; gap: 6px; align-items: center; color: var(--muted); }
select { font: inherit; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; }
th:nth-child(2), td:nth-child(2) { text-align: left; }
th { font-size: 12px; color: var(--muted); font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; }
td a { color: var(--accent); text-decoration: none; }
td a:hover { text-decoration: underline; }
.desc { color: var(--muted); font-size: 12px; display: block; max-width: 52ch;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { display: inline-block; padding: 0 6px; margin: 0 4px 2px 0; border: 1px solid var(--line);
  border-radius: 999px; font-size: 11px; color: var(--muted); }
.warn { color: #b45309; }
.muted { color: var(--muted); }
footer { padding: 0 clamp(16px, 4vw, 48px) 40px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>Rising Stars AI</h1>
  <p class="sub">__SUBTITLE__</p>
  <dl class="meta" id="meta"></dl>
</header>
<main>
  <div class="controls">
    <label>榜单 <select id="board"></select></label>
    <label>领域 <select id="domain"></select></label>
    <label>技术形态 <select id="form"></select></label>
    <label>项目类型 <select id="kind"></select></label>
    <span class="muted" id="count"></span>
  </div>
  <table>
    <thead><tr>
      <th>#</th><th>项目</th><th>总 Star</th><th>本期新增</th><th>上期新增</th>
      <th>速度</th><th>加速度</th><th>综合分</th><th>标签</th><th>完整性</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</main>
<footer>数据可离线重算：JSON 与 CSV 保留原始指标；综合分只含增长与加速度的百分位。</footer>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById("payload").textContent);
const ALL = "__ALL__";
const fmt = (value, digits) => (value === null || value === undefined) ? "—" :
  Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits === undefined ? 2 : digits });
const fill = (id, values, withAll) => {
  const select = document.getElementById(id);
  if (withAll) {
    const option = document.createElement("option");
    option.value = ALL;
    option.textContent = "全部";
    select.append(option);
  }
  [...new Set(values)].forEach(value => {
    const option = document.createElement("option");
    option.value = option.textContent = value;
    select.append(option);
  });
};
const ORDER = ["daily_growth", "daily_acceleration", "daily_score", "weekly_growth",
  "weekly_acceleration", "weekly_score", "monthly_growth", "monthly_acceleration",
  "monthly_score", "newcomers"];
const unique = values => [...new Set(values)].sort();
const orderRank = name => ORDER.includes(name) ? ORDER.indexOf(name) : ORDER.length;
const boardNames = Object.keys(data.boards).sort((a, b) => orderRank(a) - orderRank(b));
fill("board", boardNames, false);
fill("domain", unique(boardNames.flatMap(name => data.boards[name].items.flatMap(item => item.domains))), true);
fill("form", unique(boardNames.flatMap(name => data.boards[name].items.flatMap(item => item.forms))), true);
fill("kind", unique(boardNames.flatMap(name => data.boards[name].items.flatMap(item => item.kinds))), true);

const meta = data.meta;
for (const [label, value] of [
  ["监控池", meta.pool.repos + " 个仓库（常规 " + meta.pool.regular + " / 新锐 " + meta.pool.newcomer + "）"],
  ["统计窗口", "截至 " + data.end_day],
  ["数据更新", meta.generated_at],
  ["缺测", meta.pool.no_history ? meta.pool.no_history + " 个仓库无历史数据" : "无缺失历史"],
]) {
  const wrap = document.createElement("div");
  wrap.innerHTML = "<dt>" + label + "</dt><dd>" + value + "</dd>";
  document.getElementById("meta").append(wrap);
}

function render() {
  const board = data.boards[document.getElementById("board").value];
  const domain = document.getElementById("domain").value;
  const form = document.getElementById("form").value;
  const kind = document.getElementById("kind").value;
  const items = board.items.filter(item =>
    (!domain || domain === ALL || item.domains.includes(domain)) &&
    (!form || form === ALL || item.forms.includes(form)) &&
    (!kind || kind === ALL || item.kinds.includes(kind)));
  document.getElementById("count").textContent =
    board.board_label + " · 显示 " + items.length + " 项（入榜 " + board.items.length + " 项）";
  document.getElementById("rows").innerHTML = items.map(item => {
    const tags = [...item.domains, ...item.forms.map(tag => "形·" + tag), ...item.kinds.map(tag => "类·" + tag)]
      .map(tag => '<span class="tag">' + tag + "</span>").join("");
    const desc = item.description ? '<span class="desc">' + item.description + "</span>" : "";
    const integrity = item.complete ? "完整" :
      '<span class="warn">可用 ' + item.days_available + " 天</span>";
    return "<tr>" +
      '<td class="num">' + item.rank + "</td>" +
      '<td><a href="' + item.url + '" target="_blank" rel="noopener">' + item.full_name + "</a>" + desc + "</td>" +
      '<td class="num">' + fmt(item.stars, 0) + "</td>" +
      '<td class="num">' + fmt(item.added, 0) + "</td>" +
      '<td class="num">' + fmt(item.added_previous, 0) + "</td>" +
      '<td class="num">' + fmt(item.velocity) + "</td>" +
      '<td class="num">' + fmt(item.acceleration) + "</td>" +
      '<td class="num">' + fmt(item.score) + "</td>" +
      "<td>" + tags + "</td>" +
      "<td>" + integrity + "</td>" +
      "</tr>";
  }).join("");
}
["board", "domain", "form", "kind"].forEach(id =>
  document.getElementById(id).addEventListener("change", render));
render();
</script>
</body>
</html>
"""


def render_page(boards: dict[str, dict], pool: dict, end_day: str, generated_at: str) -> str:
    payload = json.dumps({"meta": {"pool": pool, "generated_at": generated_at}, "end_day": end_day,
                          "boards": boards}, ensure_ascii=False).replace("</", "<\\/")
    subtitle = f"截至 {end_day} · {pool['repos']} 个监控仓库 · 数据更新 {generated_at}"
    return PAGE.replace("__SUBTITLE__", subtitle).replace("__PAYLOAD__", payload)
