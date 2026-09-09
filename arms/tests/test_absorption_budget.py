from research_arms.absorption import budget_summary


def test_large_paper_budget_warns_without_altering_plan():
    plan = {"tasks": ["methods", "math", "embeddings"], "previews": [
        {"task": "methods", "expected_calls": 72}, {"task": "math", "expected_calls": 139}],
        "source_embedding_records": 611, "limits": {"max_calls": 32}}
    result = budget_summary(plan)
    assert result["source_embedding_calls"] == 10
    assert result["uncached_minimum_calls"] == 221
    assert result["below_uncached_baseline"]
    assert plan["limits"]["max_calls"] == 32
    plan["limits"]["max_calls"] = 256
    assert not budget_summary(plan)["below_uncached_baseline"]
    assert "Token feasibility is not estimated" in result["notice"]


def test_missing_preview_is_unknown_not_zero():
    assert budget_summary({}) == {"available": False}
