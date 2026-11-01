import os

from rising_stars_ai import __main__ as cli


def test_load_dotenv_fills_missing_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nGITHUB_TOKEN=from_file\nOTHER=from_file\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("OTHER", "from_env")
    cli._load_dotenv(str(env))
    assert os.environ["GITHUB_TOKEN"] == "from_file"
    assert os.environ["OTHER"] == "from_env"


def test_daily_without_star_history_exits_nonzero(tmp_path, monkeypatch):
    # 空库跑 daily：没有 Star 历史就没有榜单，流水线必须以失败退出，别让 CI 绿着发空站
    for name in ("run_search", "run_trending", "run_ossinsight"):
        monkeypatch.setattr(cli.discover, name, lambda *args, **kwargs: {})
    assert cli.main(["--db", str(tmp_path / "empty.db"), "daily"]) == 1
