import json

import pytest

from research_arms.publication import Publications
from research_arms import Unavailable
from test_registry import setup


def note(arms, body="Curated selected text", space="alice"):
    return arms.spaces.open(space).create_research_object(kind="note", body=body,
        origin="AGENT_INTERPRETED", review_state="UNREVIEWED").id


def test_preview_is_private_immutable_and_consent_precedes_approval(setup):
    arms, spaces = setup
    spaces.set_membership("project", "bob", "maintainer")
    ident = note(arms)
    publications = Publications(arms)
    before = spaces.open("project").list_research_objects()
    preview = publications.prepare("1", "alice", "project", note_ids=[ident])
    pid, digest = preview["publication_id"], preview["digest"]
    encoded = json.dumps(preview)
    assert ident not in encoded and "PRIVATE_CANARY" not in encoded and str(arms.root) not in encoded
    assert preview["bundle"]["notes"][0]["origin"] == "AGENT_INTERPRETED"
    with pytest.raises(Unavailable): publications.show("2", pid)
    with pytest.raises(Unavailable): publications.decide("2", pid, digest, "approve")
    with pytest.raises(ValueError): publications.decide("1", pid, "wrong", "consent")
    publications.decide("1", pid, digest, "consent")
    assert publications.show("2", pid)["bundle"] == preview["bundle"]
    publications.decide("2", pid, digest, "approve")
    publications.decide("2", pid, digest, "approve")
    assert spaces.open("project").list_research_objects() == before  # No publication executor yet.
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='publication_approve'").fetchone()[0] == 1


def test_nonmaintainer_owner_needs_destination_approval_and_can_cancel(setup):
    arms, _ = setup
    publications = Publications(arms)
    preview = publications.prepare("2", "bob", "project", note_ids=[note(arms, space="bob")])
    pid, digest = preview["publication_id"], preview["digest"]
    with pytest.raises(Unavailable): publications.decide("1", pid, digest, "consent")
    publications.decide("2", pid, digest, "consent")
    with pytest.raises(PermissionError): publications.decide("2", pid, digest, "approve")
    publications.decide("1", pid, digest, "approve")
    publications.decide("2", pid, digest, "cancel")
    with pytest.raises(Unavailable): publications.show("1", pid)


def test_private_references_and_nonowner_selection_rejected(setup):
    arms, _ = setup
    publications = Publications(arms)
    ident = note(arms, "See obj_private or /Users/private/session.jsonl")
    with pytest.raises(ValueError, match="private references"):
        publications.prepare("1", "alice", "project", note_ids=[ident])
    with pytest.raises(PermissionError): publications.prepare("2", "alice", "project", note_ids=[ident])
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_policy_change_invalidates_pending_publication(setup):
    arms, spaces = setup
    publications = Publications(arms)
    preview = publications.prepare("1", "alice", "project", note_ids=[note(arms)])
    spaces.set_membership("project", "bob", None)
    with pytest.raises(Unavailable): publications.decide("1", preview["publication_id"], preview["digest"], "consent")


def test_paper_dependencies_use_bundle_aliases_not_private_ids(setup):
    arms, spaces = setup
    brain = spaces.open("alice")
    source = brain.root / "synthetic.md"
    source.write_text("# Synthetic paper\n\nEvidence passage.\n")
    ingested = brain.ingest(source)
    with brain.store.connect() as db:
        block = db.execute("SELECT id FROM document_blocks WHERE block_type='paragraph'").fetchone()[0]
        # Synthetic metadata only; no public source or network call is claimed.
        db.execute("UPDATE source_assets SET uri='https://arxiv.org/src/2506.24056v2'")
        db.execute("UPDATE document_versions SET version_label='v2'")
    obj = brain.create_research_object(kind="note", body="Selected evidence interpretation", origin="AGENT_INTERPRETED",
        review_state="UNREVIEWED", evidence=[(block, "source_context_only", None)])
    preview = Publications(arms).prepare("1", "alice", "project", note_ids=[obj.id], paper_block_ids=[block])
    bundle = preview["bundle"]
    assert len(bundle["papers"]) == len(bundle["evidence"]) == 1
    assert bundle["notes"][0]["evidence"] == [{"ref": "evidence_1", "relation": "source_context_only"}]
    assert bundle["evidence"][0]["text"] == brain.get_evidence(block)["raw_text"]
    encoded = json.dumps(bundle)
    for private in (obj.id, block, ingested.compilation_id, ingested.document_id, str(source)):
        assert private not in encoded
