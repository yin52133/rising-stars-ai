from rising_stars_ai import db

LATER = "2026-11-02T00:00:00+00:00"


def test_unknown_stars_do_not_overwrite_known_value(con, add_repo):
    add_repo(1, stars=1200)
    db.upsert_repository(con, {"repo_id": 1, "full_name": "acme/repo1"}, LATER)
    row = con.execute("SELECT stars, last_seen FROM repositories WHERE repo_id = 1").fetchone()
    assert row["stars"] == 1200
    assert row["last_seen"] == LATER


def test_repository_deduped_by_id_and_renamed(con, add_repo):
    add_repo(1, stars=900)
    db.upsert_repository(con, {"repo_id": 1, "full_name": "acme/renamed", "stars": 950}, LATER)
    rows = con.execute("SELECT full_name, stars FROM repositories").fetchall()
    assert [(row["full_name"], row["stars"]) for row in rows] == [("acme/renamed", 950)]


def test_membership_promotion_keeps_history(con, add_repo):
    add_repo(1)
    db.sync_memberships(con, {1: ("regular", "domain:编程 基础名额")}, "2026-10-25T00:00:00+00:00")
    db.sync_memberships(con, {1: ("newcomer", "新锐观察")}, "2026-11-01T00:00:00+00:00")
    rows = con.execute("SELECT pool, reason, valid_to FROM pool_memberships ORDER BY valid_from").fetchall()
    assert len(rows) == 2
    assert rows[0]["valid_to"] is not None and "晋级" in rows[0]["reason"]
    assert rows[1]["pool"] == "newcomer" and rows[1]["valid_to"] is None


def test_membership_removal_records_reason(con, add_repo):
    add_repo(1)
    db.sync_memberships(con, {1: ("regular", "机动名额")}, "2026-10-25T00:00:00+00:00")
    result = db.sync_memberships(con, {}, "2026-11-01T00:00:00+00:00")
    row = con.execute("SELECT reason, valid_to FROM pool_memberships").fetchone()
    assert result["demoted"] == 1
    assert row["valid_to"] is not None and "移出" in row["reason"]


def test_source_evidence_deduped(con, add_repo):
    add_repo(1)
    db.add_source(con, 1, "github_search", "topic:llm|stars:500..2000", "https://x/1", "{}", LATER)
    db.add_source(con, 1, "github_search", "topic:llm|stars:500..2000", "https://x/2", "{}", LATER)
    rows = con.execute("SELECT url, first_seen, last_seen FROM discovery_sources").fetchall()
    assert len(rows) == 1 and rows[0]["url"] == "https://x/2"


def test_star_day_upsert_is_idempotent(con, add_repo):
    add_repo(1)
    db.upsert_star_days(con, 1, [{"day": "2026-10-30", "added": 3, "week": "2026-W44"}],
                        "github_history", "run-1", LATER)
    db.upsert_star_days(con, 1, [{"day": "2026-10-30", "added": 5, "week": "2026-W44"}],
                        "github_history", "run-2", LATER)
    rows = con.execute("SELECT added, run_id FROM star_history_days").fetchall()
    assert [(row["added"], row["run_id"]) for row in rows] == [(5, "run-2")]
