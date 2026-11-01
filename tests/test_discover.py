from datetime import date

from rising_stars_ai import discover
from rising_stars_ai.api import ApiError
from rising_stars_ai.config import sources

SEEN = "2026-11-01T00:00:00+00:00"
TRENDING_HTML = """
<article><h2 class="h3 lh-condensed"><a href="/acme/agent-kit" data-view-component="true">acme / agent-kit</a></h2></article>
<article><h2 class="h3 lh-condensed"><a href="/beta/video-gen">beta / video-gen</a></h2></article>
<article><h2 class="h3 lh-condensed"><a href="/acme/agent-kit">dup</a></h2></article>
"""


class FakeClient:
    def __init__(self, html: str = TRENDING_HTML, failing: tuple[str, ...] = ()):
        self.html = html
        self.failing = set(failing)
        self.requests = 0
        self.errors = 0
        self.ids = {"acme/agent-kit": 11, "beta/video-gen": 12}

    def get_text(self, url, accept="text/html"):
        self.requests += 1
        return self.html

    def get_json(self, url, params=None, accept="application/vnd.github+json"):
        self.requests += 1
        name = url.split("/repos/", 1)[1]
        if name in self.failing:
            self.errors += 1
            raise ApiError(name)
        return {"id": self.ids[name], "full_name": name, "html_url": f"https://github.com/{name}",
                "stargazers_count": 900, "created_at": "2025-01-01T00:00:00Z",
                "pushed_at": "2026-10-30T00:00:00Z", "topics": ["llm"],
                "archived": False, "fork": False}


def test_search_queries_split_by_topic_star_and_creation():
    queries = discover.build_queries(sources(), date(2026, 11, 1))
    keys = [query["key"] for query in queries]
    assert len(keys) == len(set(keys))
    assert any(key.startswith("topic:llm|stars:500..2000") for key in keys)
    assert any(key.startswith("kw:llm|created:<2024-06-01") for key in keys)
    assert all("archived:false" in query["q"] for query in queries)
    assert all("pushed:>=2025-11-01" in query["q"] for query in queries if query["key"].startswith("topic:"))


def test_parse_trending_dedupes_in_order():
    assert discover.parse_trending(TRENDING_HTML) == ["acme/agent-kit", "beta/video-gen"]


def test_trending_stores_candidates_with_source(con):
    client = FakeClient()
    result = discover.run_trending(con, client, sources(), "run-1", SEEN)
    assert result["repos"] == 2 and result["errors"] == 0
    rows = con.execute("SELECT full_name, stars FROM repositories ORDER BY repo_id").fetchall()
    assert [(row["full_name"], row["stars"]) for row in rows] == [
        ("acme/agent-kit", 900), ("beta/video-gen", 900)]
    sources_rows = con.execute(
        "SELECT source, source_key, evidence FROM discovery_sources ORDER BY source_key").fetchall()
    assert {row["source"] for row in sources_rows} == {"github_trending"}
    assert all("trending:" in row["source_key"] for row in sources_rows)
    assert all('"since"' in row["evidence"] for row in sources_rows)


def test_trending_failure_records_no_repository(con):
    client = FakeClient(failing=("beta/video-gen",))
    result = discover.run_trending(con, client, sources(), "run-1", SEEN)
    assert result["repos"] == 1 and result["errors"] == 2
    names = [row["full_name"] for row in con.execute("SELECT full_name FROM repositories")]
    assert names == ["acme/agent-kit"]


def test_trending_disabled_returns_early(con):
    cfg = {"github_trending": {"enabled": False}}
    assert discover.run_trending(con, FakeClient(), cfg, "run-1", SEEN) == {"enabled": False}


def test_ossinsight_disabled_by_default(con):
    assert discover.run_ossinsight(con, FakeClient(), sources(), "run-1", SEEN) == {"enabled": False}
