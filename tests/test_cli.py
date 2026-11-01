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
