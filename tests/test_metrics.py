from datetime import date, timedelta

import pytest

from rising_stars_ai import metrics

END = date(2026, 10, 31)


def test_daily_acceleration(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [20, 10])
    metric = metrics.compute(con, 1, 1, END)
    assert metric["added_current"] == 20
    assert metric["added_previous"] == 10
    assert metric["velocity"] == 20
    assert metric["acceleration"] == 10
    assert metric["complete"] == 1


def test_daily_decline_keeps_negative_acceleration(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [5, 25])
    metric = metrics.compute(con, 1, 1, END)
    assert metric["velocity"] == 5
    assert metric["acceleration"] == -20


def test_weekly_windows_ignore_days_outside_range(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [5] * 7 + [2] * 7 + [1000])
    # 窗口之外的一天不应进入统计
    add_days(1, END + timedelta(days=1), [999])
    metric = metrics.compute(con, 1, 7, END)
    assert metric["added_current"] == 35
    assert metric["added_previous"] == 14
    assert metric["velocity"] == 5
    assert metric["acceleration"] == pytest.approx((5 - 2) / 7)


def test_monthly_requires_sixty_days(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [1] * 59)
    metric = metrics.compute(con, 1, 30, END)
    assert metric["added_current"] == 30       # 最近 30 天齐全，可上绝对增长榜
    assert metric["acceleration"] is None      # 不足 60 天，没有加速度
    assert metric["complete"] == 0

    add_days(1, END, [1] * 60)
    metric = metrics.compute(con, 1, 30, END)
    assert metric["added_current"] == 30
    assert metric["added_previous"] == 30
    assert metric["acceleration"] == 0.0
    assert metric["complete"] == 1


def test_missing_day_is_not_counted_as_zero(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [3] * 14)
    con.execute("DELETE FROM star_history_days WHERE repo_id = 1 AND day = ?",
                ((END - timedelta(days=3)).isoformat(),))
    metric = metrics.compute(con, 1, 7, END)
    assert metric["added_current"] is None
    assert metric["complete"] == 0
    assert metric["days_available"] == 13


@pytest.mark.parametrize("window", [1, 7, 30])
def test_minimum_history_requirement(con, add_repo, add_days, window):
    add_repo(2)
    add_days(2, END, [1] * (2 * window - 1))
    metric = metrics.compute(con, 2, window, END)
    assert metric["added_current"] == window   # 本期窗口齐全
    assert metric["acceleration"] is None      # 上期缺一天，不做加速度
    assert metric["complete"] == 0

    add_days(2, END, [1] * (2 * window))
    metric = metrics.compute(con, 2, window, END)
    assert metric["acceleration"] == 0.0
    assert metric["complete"] == 1


def test_resolve_end_day_uses_latest_day(con, add_repo, add_days):
    add_repo(1)
    add_days(1, END, [1, 2, 3])
    assert metrics.resolve_end_day(con) == END
