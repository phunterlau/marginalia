"""Synthetic human-review protocol tests; no real corpus acceptance."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from research_arms.reviews import CardReviews
from research_arms import Unavailable
from test_registry import setup


def card(arms, space="project"):
    brain = arms.spaces.open(space)
    source = brain.root / "synthetic.md"
    source.write_text("# Synthetic\n\nMatched activations give a contrast direction.\n")
    brain.ingest(source)
    with brain.store.connect() as db:
        block = db.execute("SELECT id FROM document_blocks WHERE block_type='paragraph' LIMIT 1").fetchone()[0]
    obj = brain.create_research_object(kind="method_card", title="Synthetic contrast", body="Contrast direction",
        structured={"problem": "Synthetic review fixture"}, origin="AGENT_EXTRACTED", review_state="UNREVIEWED",
        evidence=[(block, "source_context_only", None)])
    return brain, obj.id


def test_review_preserves_content_origin_and_changes_reliable_recall(setup):
    arms, _ = setup
    brain, ident = card(arms)
    service = CardReviews(arms, "1", "project")
    original = service.show(ident)["result"]
    assert not any(hit.record_id == ident for hit in brain.recall("Contrast direction"))
    result = service.decide(ident, "ACCEPTED", original["updated_at"])
    assert result["origin"] == "AGENT_EXTRACTED" and result["review_state"] == "ACCEPTED"
    updated = brain.get_research_object(ident)
    for key in ("body", "structured", "origin", "evidence"): assert updated[key] == original[key]
    assert any(hit.record_id == ident for hit in brain.recall("Contrast direction"))
    with pytest.raises(ValueError, match="conflict"): service.decide(ident, "ACCEPTED", original["updated_at"])
    with brain.store.connect() as db:
        events = db.execute("SELECT actor FROM events WHERE event_type='object_reviewed' AND object_id=?", (ident,)).fetchall()
        assert len(events) == 1 and events[0][0] == "discord:1"


def test_member_and_wrong_space_cannot_review(setup):
    arms, _ = setup
    brain, ident = card(arms)
    version = brain.get_research_object(ident)["updated_at"]
    assert CardReviews(arms, "2", "project").show(ident)["result"]["id"] == ident
    with pytest.raises(PermissionError): CardReviews(arms, "2", "project").decide(ident, "ACCEPTED", version)
    with pytest.raises(Unavailable): CardReviews(arms, "1", "alice").show(ident)
    assert brain.get_research_object(ident)["review_state"] == "UNREVIEWED"


def test_concurrent_decisions_and_required_notes(setup):
    arms, _ = setup
    brain, ident = card(arms)
    version = brain.get_research_object(ident)["updated_at"]
    service = CardReviews(arms, "1", "project")
    for decision in ("REJECTED", "DISPUTED"):
        with pytest.raises(ValueError, match="note"): service.decide(ident, decision, version)
    def decide(decision):
        try: return service.decide(ident, decision, version, "Synthetic test decision")["review_state"]
        except ValueError: return "CONFLICT"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(decide, ["ACCEPTED", "DISPUTED"]))
    assert results.count("CONFLICT") == 1


def test_revoked_review_scope_fails_before_write(setup):
    arms, spaces = setup
    brain, ident = card(arms)
    spaces.set_membership("project", "bob", "maintainer")
    service = CardReviews(arms, "2", "project")
    version = service.show(ident)["result"]["updated_at"]
    spaces.set_membership("project", "bob", "member")
    with pytest.raises(PermissionError): service.decide(ident, "ACCEPTED", version)
    assert brain.get_research_object(ident)["review_state"] == "UNREVIEWED"
