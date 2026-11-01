import csv
import json

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


def test_page_embeds_payload_and_meta(con, add_repo, tmp_path):
    seed(con, add_repo, 1, 100, 5.0)
    rank.build(con, CFG, END)
    out = tmp_path / "out"
    publish.publish(con, CFG, END, out_dir=out, generated_at="2026-11-01T00:00:00+00:00")

    page = (out / "index.html").read_text(encoding="utf-8")
    assert "Rising Stars AI" in page
    assert "__PAYLOAD__" not in page and "__SUBTITLE__" not in page
    assert f"截至 {END}" in page
    assert '"daily_growth"' in page.replace(": ", ": ")

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["end_day"] == END and meta["pool"]["repos"] == 1
