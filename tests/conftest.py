from datetime import date, timedelta

import pytest

from rising_stars_ai import db

SEEN = "2026-11-01T00:00:00+00:00"


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def add_repo(con):
    def _add(repo_id: int, **overrides) -> dict:
        repo = {
            "repo_id": repo_id,
            "full_name": f"acme/repo{repo_id}",
            "description": "llm tool",
            "topics": ["llm"],
            "stars": 1000,
            "created_at": "2024-01-01T00:00:00Z",
            "pushed_at": "2026-10-30T00:00:00Z",
            "archived": False,
            "is_fork": False,
            "is_template": False,
        }
        repo.update(overrides)
        db.upsert_repository(con, repo, SEEN)
        return repo

    return _add


@pytest.fixture
def add_days(con):
    def _add(repo_id: int, end: date, added: list[int]) -> None:
        rows = [{"day": (end - timedelta(days=offset)).isoformat(), "added": value}
                for offset, value in enumerate(added)]
        db.upsert_star_days(con, repo_id, rows, "test", "run-test", SEEN)

    return _add
