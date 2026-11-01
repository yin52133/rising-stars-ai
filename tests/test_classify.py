from rising_stars_ai import classify
from rising_stars_ai.config import taxonomy

SEEN = "2026-11-01T00:00:00+00:00"
TAX = taxonomy()


def repo(full_name: str, description: str, topics: list[str]) -> dict:
    return {"repo_id": 1, "full_name": full_name, "description": description, "topics": topics}


def test_ai_tool_is_included_with_labels():
    result = classify.classify(repo("acme/agent-kit", "Coding agent harness for LLM workflows",
                                    ["llm", "agents"]), TAX)
    assert result["included"] is True
    assert "Agent" in result["domains"]
    assert "Harness" in result["forms"]
    assert result["kind"] == "tool"
    assert result["confidence"] > 0.5


def test_awesome_list_marked_resource_list():
    result = classify.classify(repo("acme/awesome-llm", "Curated list of LLM resources",
                                    ["awesome", "llm"]), TAX)
    assert result["kind"] == "resource_list"
    assert result["included"] is True


def test_unrelated_project_excluded():
    result = classify.classify(repo("acme/redis-tools", "Redis utilities for server operators",
                                    ["redis", "ops"]), TAX)
    assert result["included"] is False
    assert result["confidence"] < 0.3


def test_short_keyword_needs_word_boundary():
    result = classify.classify(repo("acme/maintainer-scripts", "Scripts to maintain servers", []), TAX)
    assert result["included"] is False


def test_classify_pending_writes_labels(con, add_repo):
    add_repo(1, description="LLM agent harness", topics=["llm", "agents"])
    summary = classify.classify_pending(con, TAX, SEEN)
    assert summary == {"checked": 1, "included": 1}
    labels = {row["label"] for row in con.execute(
        "SELECT label FROM repository_labels WHERE repo_id = 1")}
    assert {"Agent", "tool"} <= labels
    row = con.execute("SELECT ai_included FROM repositories WHERE repo_id = 1").fetchone()
    assert row["ai_included"] == 1


def test_classify_pending_is_idempotent(con, add_repo):
    add_repo(1, description="LLM agent harness", topics=["llm"])
    classify.classify_pending(con, TAX, SEEN)
    assert classify.classify_pending(con, TAX, SEEN)["checked"] == 0
