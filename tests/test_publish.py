import csv
import json
from html.parser import HTMLParser

from rising_stars_ai import db, publish, rank

END = "2026-10-31"
CFG = {"rank": {"top_n": 100, "newcomer_days": 180}, "publish": {"out_dir": "unused"}}


def seed(con, add_repo, repo_id: int, added: int, accel: float, complete: int = 1) -> None:
    add_repo(repo_id, description="LLM agent harness", topics=["llm", "agents"])
    db.set_labels(con, repo_id, [("domain", "Agent", "test", 0.8), ("form", "Harness", "test", 0.8),
                                 ("kind", "tool", "test", 0.8)], "rule")
    con.execute(
        "INSERT INTO pool_memberships (repo_id, valid_from, valid_to, pool, reason)"
        " VALUES (?, '2026-10-30T00:00:00+00:00', NULL, 'regular', 'test')", (repo_id,))
    for window in (1, 7, 30):
        con.execute(
            "INSERT INTO metric_windows (repo_id, window, end_day, added_current, added_previous,"
            " velocity, acceleration, complete, days_available) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, window, END, added * window, added * window, float(added), accel, complete, 2 * window),
        )


def test_publish_writes_boards_csv_and_page(con, add_repo, tmp_path):
    for repo_id, added, accel in ((1, 100, 5.0), (2, 40, 1.0), (3, 10, 0.5)):
        seed(con, add_repo, repo_id, added, accel)
    out = tmp_path / "rankings"
    rank.build(con, CFG, END)
    result = publish.publish(con, CFG, END, out_dir=out, generated_at="2026-11-01T00:00:00+00:00")

    names = {path.name for path in out.iterdir()}
    expected = {f"{period}_{board}_top100.json"
                for period in ("daily", "weekly", "monthly")
                for board in ("growth", "acceleration", "score")}
    assert expected <= names
    assert {"meta.json", "index.html", "newcomers_top100.json", f"rankings_{END}.csv"} <= names
    assert result["boards"]["daily_growth"] == 3
    assert result["boards"]["newcomers"] == 0

    payload = json.loads((out / "daily_growth_top100.json").read_text(encoding="utf-8"))
    assert payload["board"] == "daily_growth" and payload["window"] == 1
    assert payload["pool"]["repos"] == 3
    assert payload["incomplete_items"] == 0
    first = payload["items"][0]
    assert first["full_name"] == "acme/repo1" and first["rank"] == 1
    assert first["added"] == 100 and first["velocity"] == 100.0
    assert first["domains"] == ["Agent"] and first["kinds"] == ["tool"]
    assert first["complete"] is True and first["stars"] == 1000


def test_publish_keeps_raw_metrics_and_marks_incomplete(con, add_repo, tmp_path):
    seed(con, add_repo, 1, 100, 5.0, complete=0)
    rank.build(con, CFG, END)
    out = tmp_path / "out"
    publish.publish(con, CFG, END, out_dir=out)

    payload = json.loads((out / "daily_score_top100.json").read_text(encoding="utf-8"))
    assert payload["incomplete_items"] == 1
    item = payload["items"][0]
    assert item["complete"] is False and item["days_available"] == 2
    assert {"added", "added_previous", "velocity", "acceleration", "p_added", "p_accel",
            "score"} <= set(item)

    with open(out / f"rankings_{END}.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and {"board", "added", "acceleration", "score", "complete", "domains"} <= set(rows[0])
    assert {row["board"] for row in rows} >= {"daily_growth", "daily_score", "monthly_acceleration"}


def test_pool_dataset_covers_whole_pool_with_tags_and_windows(con, add_repo, tmp_path):
    for repo_id in (1, 2, 3):
        seed(con, add_repo, repo_id, added=repo_id * 10, accel=float(repo_id))
    con.execute("UPDATE repositories SET created_at = '2026-10-01T00:00:00Z' WHERE repo_id = 3")
    con.execute("UPDATE repositories SET url = 'https://github.com/acme/repo1' WHERE repo_id = 1")
    con.execute("UPDATE pool_memberships SET pool = 'newcomer' WHERE repo_id = 3")

    out = tmp_path / "out"
    rank.build(con, CFG, END)
    publish.publish(con, CFG, END, out_dir=out, generated_at="2026-11-01T00:00:00+00:00")

    rows = json.loads((out / "pool.json").read_text(encoding="utf-8"))
    assert [row["repo_id"] for row in rows] == [1, 2, 3]
    first = rows[0]
    assert first["domains"] == ["Agent"] and first["forms"] == ["Harness"] and first["kinds"] == ["tool"]
    assert first["url"] == "https://github.com/acme/repo1"
    assert sorted(first["windows"], key=int) == ["1", "7", "30"]
    assert first["windows"]["1"]["added"] == 10
    assert first["windows"]["1"]["score"] is not None and first["windows"]["1"]["complete"] is True
    assert [row["newcomer"] for row in rows] == [False, False, True]


def test_page_preserves_untrusted_description_without_executing_markup(con, add_repo, tmp_path):
    seed(con, add_repo, 1, 100, 5.0)
    description = '</script><script>alert("injected")</script>'
    con.execute("UPDATE repositories SET description = ? WHERE repo_id = 1", (description,))
    rank.build(con, CFG, END)
    out = tmp_path / "out"
    publish.publish(con, CFG, END, out_dir=out, generated_at="2026-11-01T00:00:00+00:00")

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []
            self.current = None

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.current = ""

        def handle_data(self, data):
            if self.current is not None:
                self.current += data

        def handle_endtag(self, tag):
            if tag == "script" and self.current is not None:
                self.scripts.append(self.current)
                self.current = None

    page = Scripts()
    page.feed((out / "index.html").read_text(encoding="utf-8"))
    assert len(page.scripts) == 2
    payload = json.loads(page.scripts[0])
    assert payload["boards"]["daily_growth"]["items"][0]["description"] == description
    assert payload["end_day"] == END and payload["meta"]["pool"]["repos"] == 1
