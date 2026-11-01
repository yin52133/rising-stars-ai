from datetime import datetime, timedelta, timezone

from rising_stars_ai.stars import parse_history


def test_parse_history_expands_week_buckets():
    """官方 /stargazers/history 返回周桶：week 是周日零点 epoch，days 是 7 天新增。"""
    rows = parse_history([{"week": 1792886400, "total": 987,
                           "days": [159, 160, 149, 121, 147, 125, 126]}])
    assert [row["added"] for row in rows] == [159, 160, 149, 121, 147, 125, 126]
    assert rows[0]["day"] == "2026-10-25"
    assert rows[-1]["day"] == "2026-10-31"


def test_parse_history_drops_future_days():
    """本周还没到的天不落地，否则会被当成 0 计入指标。"""
    today = datetime.now(timezone.utc).date()
    week_ts = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp())
    rows = parse_history([{"week": week_ts, "days": [5, 0, 0, 0, 0, 0, 0]}])
    assert rows == [{"day": today.isoformat(), "added": 5}]


def test_parse_history_keeps_legacy_shapes():
    assert parse_history({"days": [{"day": "2026-10-30", "added": 7}]}) == [
        {"day": "2026-10-30", "added": 7}]
    assert parse_history([{"day": "2026-10-29", "added": None}]) == []
