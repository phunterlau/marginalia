import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.cli import main
from research_brain.comparison import (
    compare_research,
    DIMENSIONS,
    validate_answer,
    validate_judgment,
    load_comparison_spec,
    ids_in,
)
from research_brain.frontier_evaluation import seed_frontier_fixture

SPEC = Path(__file__).parents[1] / "evals" / "comparison-v1.json"


class FakeProvider:
    calls = []

    def __init__(self, **kwargs):
        pass

    def generate(self, *, payload, **kwargs):
        self.calls.append(payload)
        output = (
            {
                "A": dict.fromkeys(DIMENSIONS, 2),
                "B": dict.fromkeys(DIMENSIONS, 2),
                "rationale": "Equal synthetic answers.",
            }
            if "A" in payload
            else {
                "answer": "Proposed: run norm-matched random controls.",
                "proposals": ["Compare held-out prompts"],
                "limitations": ["No actual experiment performed"],
                "cited_ids": [],
            }
        )
        return {
            "response_id": f"resp_{len(self.calls)}",
            "status": "completed",
            "output_text": json.dumps(output),
            "raw_output": [],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }


@pytest.fixture
def fixture(tmp_path):
    brain = Brain(tmp_path / "brain")
    thread = seed_frontier_fixture(brain)
    FakeProvider.calls = []
    return brain, thread, tmp_path / "comparison"


def test_dry_run_spends_nothing_creates_no_artifacts(fixture):
    brain, thread, out = fixture
    before = brain.store.path.read_bytes()
    result = compare_research(
        brain,
        SPEC,
        thread_id=thread,
        output_dir=out,
        provider_factory=lambda **kw: pytest.fail("provider forbidden"),
    )
    assert not result.live and result.plan["expected_calls"] == 9
    assert result.plan["maximum_attempts"] == 27
    assert not out.exists() and brain.store.path.read_bytes() == before


def test_full_matched_comparison_preserves_blindness_order_and_source(fixture):
    brain, thread, out = fixture
    before = brain.store.path.read_bytes()
    result = compare_research(
        brain,
        SPEC,
        thread_id=thread,
        fixture=True,
        live=True,
        output_dir=out,
        provider_factory=FakeProvider,
    )
    assert result.passed, result.error
    assert brain.store.path.read_bytes() == before
    assert len(FakeProvider.calls) == 9
    assert set(FakeProvider.calls[0]) == {"question", "task_brief"}
    control, assisted = FakeProvider.calls[1:3]
    assert "memory" not in control and "memory" in assisted
    assert control["initial_draft"] == assisted["initial_draft"]
    for first, second in [(5, 6), (7, 8)]:
        assert FakeProvider.calls[first]["A"] == FakeProvider.calls[second]["B"]
        assert (
            FakeProvider.calls[first]["reference_dossier"]
            == FakeProvider.calls[second]["reference_dossier"]
        )
        assert (
            FakeProvider.calls[first]["available_evidence_ids"]["A"]
            == FakeProvider.calls[second]["available_evidence_ids"]["B"]
        )
    assert any(
        not ids for ids in FakeProvider.calls[7]["available_evidence_ids"].values()
    )
    assert all(c["human_review"] == "pending" for c in result.comparisons)
    from research_brain.store import SQLiteStore

    store = SQLiteStore(out / "generation-ledger.sqlite3", initialize=False)
    with store.connect() as c:
        assert c.execute("SELECT count(*) FROM generation_attempts").fetchone()[0] == 9
        assert c.execute("SELECT count(*) FROM research_objects").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT count(*) FROM generation_runs WHERE status='complete'"
            ).fetchone()[0]
            == 9
        )
    report = json.loads((out / "report.json").read_text())
    assert report["usage"]["total_tokens"] == 1080
    assert report["source_database_unchanged"]
    with pytest.raises(FileExistsError):
        compare_research(
            brain,
            SPEC,
            thread_id=thread,
            live=True,
            output_dir=out,
            provider_factory=FakeProvider,
        )


@pytest.mark.parametrize(
    "failure", ["malformed", "refused", "invalid_id", "incomplete"]
)
def test_invalid_provider_outputs_are_ledgered_without_retry(fixture, failure):
    brain, thread, out = fixture

    class Broken(FakeProvider):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if failure == "malformed":
                result["output_text"] = "{"
            elif failure == "refused":
                result["output_text"] = ""
                result["raw_output"] = [{"type": "refusal", "refusal": "No"}]
            elif failure == "incomplete":
                result["status"] = "incomplete"
            else:
                result["output_text"] = json.dumps(
                    {
                        "answer": "See block_fabricated",
                        "cited_ids": ["block_fabricated"],
                        "proposals": [],
                        "limitations": [],
                    }
                )
            return result

    result = compare_research(
        brain,
        SPEC,
        thread_id=thread,
        live=True,
        output_dir=out,
        provider_factory=Broken,
    )
    assert not result.passed and len(FakeProvider.calls) == 1
    report = json.loads((out / "report.json").read_text())
    assert report["provider_attempts"] == 1 and report["usage"]["total_tokens"] == 120
    assert report["source_database_unchanged"]
    assert json.loads(next(out.glob("*-response.json")).read_text())["response_id"]


def test_transient_retries_explicit_fatal_errors_not_retried(fixture):
    brain, thread, out = fixture

    class Fails:
        count = 0

        def __init__(self, **kw):
            pass

        def generate(self, **kw):
            Fails.count += 1
            raise TimeoutError("synthetic timeout")

    with patch("research_brain.comparison.time.sleep"):
        result = compare_research(
            brain,
            SPEC,
            thread_id=thread,
            live=True,
            output_dir=out,
            provider_factory=Fails,
        )
    assert not result.passed and Fails.count == 3
    assert len(list(out.glob("*-started.json"))) == 3
    assert json.loads((out / "report.json").read_text())["provider_attempts"] == 3


def test_validators_fail_closed_and_cli_defaults_offline(tmp_path, capsys):
    assert ids_in({"block_id": "block_123456789abc", "block_type": "paragraph"}) == {
        "block_123456789abc"
    }
    with pytest.raises(ValueError):
        validate_answer(
            {
                "answer": "x",
                "cited_ids": ["obj_bad"],
                "proposals": [],
                "limitations": [],
            },
            set(),
        )
    with pytest.raises(ValueError):
        validate_judgment(
            {
                "A": dict.fromkeys(DIMENSIONS, True),
                "B": dict.fromkeys(DIMENSIONS, 2),
                "rationale": "x",
            },
            set(),
        )
    root = tmp_path / "untouched"
    assert main(["--root", str(root), "compare-research", str(SPEC), "--fixture"]) == 0
    assert not root.exists()
    assert json.loads(capsys.readouterr().out)["live"] is False
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema":"ResearchComparisonV1","cases":[]}')
    with pytest.raises(ValueError):
        load_comparison_spec(bad)


def test_judge_position_bias_is_reported_not_promoted(fixture):
    brain, thread, out = fixture

    class Biased(FakeProvider):
        def generate(self, **kw):
            result = super().generate(**kw)
            if "A" in kw["payload"]:
                result["output_text"] = json.dumps(
                    {
                        "A": dict.fromkeys(DIMENSIONS, 4),
                        "B": dict.fromkeys(DIMENSIONS, 0),
                        "rationale": "Always prefer first.",
                    }
                )
            return result

    result = compare_research(
        brain,
        SPEC,
        thread_id=thread,
        live=True,
        output_dir=out,
        provider_factory=Biased,
    )
    assert result.passed
    assert all(c["diagnostic_winner"] == "order_sensitive" for c in result.comparisons)


def test_nontransient_error_has_one_attempt_and_source_output_is_rejected(fixture):
    brain, thread, out = fixture

    class FatalError(Exception):
        status_code = 403

    class Fatal(FakeProvider):
        def generate(self, **kwargs):
            raise FatalError("Do not retry authorization errors")

    result = compare_research(
        brain, SPEC, thread_id=thread, live=True, output_dir=out, provider_factory=Fatal
    )
    assert not result.passed
    assert json.loads((out / "report.json").read_text())["provider_attempts"] == 1
    with pytest.raises(ValueError, match="outside"):
        compare_research(
            brain,
            SPEC,
            thread_id=thread,
            live=True,
            output_dir=brain.root / "artifacts",
            provider_factory=FakeProvider,
        )
    assert not (brain.root / "artifacts").exists()
