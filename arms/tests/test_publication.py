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
    assert spaces.open("project").list_research_objects() == before  # Approval alone never publishes.
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


def approved(arms, **selection):
    service = Publications(arms)
    preview = service.prepare("1", "alice", "project", **selection)
    args = ("1", preview["publication_id"], preview["digest"])
    service.decide(*args, "consent")
    service.decide(*args, "approve")
    return service, args


def test_execution_is_explicit_idempotent_and_never_accepts_notes(setup):
    arms, spaces = setup
    ident = note(arms)
    service, args = approved(arms, note_ids=[ident])
    result = service.execute(*args)
    assert service.execute(*args) == result
    destination = spaces.open("project")
    record = destination.get_research_object(result["note_ids"][0])
    assert record["body"] == "Curated selected text" and record["origin"] == "AGENT_INTERPRETED"
    assert record["review_state"] == "UNREVIEWED"
    assert ident not in json.dumps(record) and "PRIVATE_CANARY" not in json.dumps(record)
    assert not any(hit.record_id == record["id"] for hit in destination.recall("Curated selected text"))


def test_crash_after_destination_commit_reconciles_receipt_without_duplicates(setup, monkeypatch):
    from research_brain.store.sqlite import SQLiteStore
    arms, spaces = setup
    service, args = approved(arms, note_ids=[note(arms)])
    original = SQLiteStore.publish_notes
    def uncertain(store, *values):
        original(store, *values)
        raise OSError("simulated lost completion")
    monkeypatch.setattr(SQLiteStore, "publish_notes", uncertain)
    with pytest.raises(OSError): service.execute(*args)
    assert service.show("1", args[1])["state"] == "NEEDS_ATTENTION"
    with pytest.raises(ValueError): service.execute(*args)
    result = service.execute(*args, retry=True)  # Receipt prevents another publish_notes call.
    with spaces.open("project").store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='publication_completed'").fetchone()[0] == 1
    assert len(result["note_ids"]) == 1


def test_publication_batch_rolls_back_on_second_note_failure(setup):
    import sqlite3
    arms, spaces = setup
    service, args = approved(arms, note_ids=[note(arms, "first"), note(arms, "second")])
    destination = spaces.open("project")
    before = destination.list_research_objects()
    with destination.store.connect() as db:
        db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON research_objects WHEN NEW.body='second' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError): service.execute(*args)
    assert destination.list_research_objects() == before
    assert destination.publication_receipt(args[2]) is None


def test_paper_source_import_resolves_exact_destination_evidence(setup):
    import io
    import tarfile
    from research_brain.ingest import ResolvedSource
    arms, spaces = setup
    source = spaces.open("alice")
    stream = io.BytesIO()
    tex = b"\\documentclass{article}\n\\begin{document}\n\\section{Synthetic}\nEvidence passage.\n\\end{document}\n"
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("main.tex")
        member.size = len(tex)
        archive.addfile(member, io.BytesIO(tex))
    ingested = source.ingestor._ingest_resolved(ResolvedSource(data=stream.getvalue(),
        uri="https://arxiv.org/src/2506.24056v2", name="synthetic.tar.gz", content_type="application/gzip",
        kind="source_archive", canonical_document_uri="https://arxiv.org/abs/2506.24056", version_label="v2",
        external_ids={"arxiv": "2506.24056"}))
    blocks = source.compilation_blocks(ingested.document_id, ingested.compilation_id)
    block = next(b for b in blocks if b["block_type"] == "paragraph")
    obj = source.create_research_object(kind="note", body="Selected source-backed interpretation",
        origin="AGENT_INTERPRETED", review_state="UNREVIEWED", evidence=[(block["id"], "source_context_only", None)])
    service, args = approved(arms, note_ids=[obj.id])
    result = service.execute(*args)
    destination = spaces.open("project")
    record = destination.get_research_object(result["note_ids"][0])
    target = destination.get_evidence(record["evidence"][0]["block_id"])
    assert target["raw_text"] == block["raw_text"] and target["version_label"] == "v2"
    assert target["source_sha256"] == ingested.sha256
    assert target["source_path"].startswith(str(destination.root))
    assert str(source.root) not in json.dumps(record)
