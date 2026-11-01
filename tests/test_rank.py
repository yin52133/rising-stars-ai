import pytest

from rising_stars_ai import rank

END = "2026-10-31"
CFG = {"rank": {"top_n": 100, "newcomer_days": 180}}


def add_metric(con, repo_id: int, window: int, added, accel, end: str = END) -> None:
    con.execute(
        "INSERT OR REPLACE INTO metric_windows (repo_id, window, end_day, added_current, added_previous,"
        " velocity, acceleration, complete, days_available) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (repo_id, window, end, added, added, None if added is None else added / window, accel, 2 * window),
    )


def add_member(con, repo_id: int, pool: str = "regular") -> None:
    con.execute(
        "INSERT INTO pool_memberships (repo_id, valid_from, valid_to, pool, reason)"
        " VALUES (?, '2026-10-30T00:00:00+00:00', NULL, ?, 'test')",
        (repo_id, pool),
    )


def test_percentile_average_rank_and_scale():
    assert rank.percentile([10, 10, 20]) == [25.0, 25.0, 100.0]
    assert rank.percentile([7]) == [100.0]
    assert rank.percentile([1, 2, 3]) == [0.0, 50.0, 100.0]
    assert rank.percentile([]) == []


def test_score_blends_equal_weight_percentiles(con, add_repo):
    for repo_id in (1, 2, 3):
        add_repo(repo_id, stars=1000 * repo_id)
        add_member(con, repo_id)
    add_metric(con, 1, 1, 100, 10)
    add_metric(con, 2, 1, 200, 5)
    add_metric(con, 3, 1, 50, 0)

    boards = rank.build(con, CFG, END)["boards"]
    daily = boards["daily_score"]
    assert [row["repo_id"] for row in daily] == [2, 1, 3]
    by_id = {row["repo_id"]: row for row in daily}
    assert by_id[1]["p_added"] == 50.0 and by_id[1]["p_accel"] == 100.0
    assert by_id[1]["score"] == 75.0
    assert by_id[2]["score"] == 75.0
    assert by_id[3]["score"] == 0.0
    assert [row["rank"] for row in daily] == [1, 2, 3]


def test_total_stars_do_not_enter_score(con, add_repo):
    add_repo(1, stars=10_000_000)
    add_repo(2, stars=1000)
    add_member(con, 1)
    add_member(con, 2)
    add_metric(con, 1, 1, 10, 0)
    add_metric(con, 2, 1, 100, 20)
    growth = rank.build(con, CFG, END)["boards"]["daily_growth"]
    assert [row["repo_id"] for row in growth] == [2, 1]


def test_top_n_limit_and_missing_metrics_excluded(con, add_repo):
    for repo_id in (1, 2, 3, 4):
        add_repo(repo_id)
        add_member(con, repo_id)
    add_metric(con, 1, 1, 40, 5)
    add_metric(con, 2, 1, 30, 4)
    add_metric(con, 3, 1, 20, 3)
    result = rank.build(con, {"rank": {"top_n": 2, "newcomer_days": 180}}, END)
    assert len(result["boards"]["daily_growth"]) == 2
    assert result["eligible"]["daily_growth"] == 3
    assert result["pool_size"] == 4
    assert "daily_acceleration" in result["boards"]


def test_newcomers_board_uses_recent_creation(con, add_repo):
    add_repo(1, created_at="2026-09-01T00:00:00Z")
    add_repo(2, created_at="2024-01-01T00:00:00Z")
    add_member(con, 1, pool="newcomer")
    add_member(con, 2, pool="newcomer")
    add_metric(con, 1, 7, 70, 1)
    add_metric(con, 2, 7, 700, 1)
    boards = rank.build(con, CFG, END)["boards"]
    assert [row["repo_id"] for row in boards["newcomers"]] == [1]
    assert boards["newcomers"][0]["rank"] == 1


def test_boards_are_persisted(con, add_repo):
    add_repo(1)
    add_member(con, 1)
    add_metric(con, 1, 1, 10, 1)
    add_metric(con, 1, 7, 70, 1)
    add_metric(con, 1, 30, 300, 1)
    rank.build(con, CFG, END)
    rows = con.execute(
        "SELECT DISTINCT window, board FROM ranking_snapshots WHERE end_day = ? ORDER BY window, board",
        (END,),
    ).fetchall()
    assert [(row["window"], row["board"]) for row in rows] == [
        (1, "acceleration"), (1, "growth"), (1, "score"),
        (7, "acceleration"), (7, "growth"), (7, "score"),
        (30, "acceleration"), (30, "growth"), (30, "score"),
    ]


def test_rebuild_replaces_snapshots(con, add_repo):
    add_repo(1)
    add_member(con, 1)
    add_metric(con, 1, 1, 10, 1)
    rank.build(con, CFG, END)
    add_metric(con, 1, 1, 12, 2)
    rank.build(con, CFG, END)
    rows = con.execute(
        "SELECT added, rank FROM ranking_snapshots WHERE end_day = ? AND window = 1 AND board = 'growth'",
        (END,),
    ).fetchall()
    assert [(row["added"], row["rank"]) for row in rows] == [(12, 1)]


def test_split_board_windows():
    assert rank.split_board("daily_growth") == (1, "growth")
    assert rank.split_board("monthly_score") == (30, "score")
    assert rank.split_board("newcomers") == (7, "newcomers")
