import pytest

from research_brain import Brain


@pytest.mark.parametrize("vector", [[float("nan")], [float("inf")], [1e100], ["bad"]])
def test_invalid_vectors_never_enter_recall(tmp_path, vector):
    class Provider:
        def __init__(self, **kw): pass
        def embed(self, texts): return [vector for _ in texts]
    brain = Brain(tmp_path / "brain", embedding_provider_factory=Provider)
    paper = tmp_path / "paper.txt"
    paper.write_text("Evidence paragraph")
    doc = brain.ingest(paper)
    with pytest.raises(ValueError):
        brain.index_embeddings(doc.document_id, live=True)
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM representations").fetchone()[0] == 0


def test_embedding_generation_completion_rolls_back_with_vectors(tmp_path):
    class Provider:
        def __init__(self, **kw): pass
        def embed(self, texts): return [[1.0, 0.0] for _ in texts]
    brain = Brain(tmp_path / "brain", embedding_provider_factory=Provider)
    paper = tmp_path / "paper.txt"
    paper.write_text("Evidence paragraph")
    doc = brain.ingest(paper)
    with brain.store.connect() as db:
        db.execute("CREATE TRIGGER fail_embedding_completion BEFORE UPDATE OF status ON generation_runs WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception, match="injected"):
        brain.index_embeddings(doc.document_id, live=True)
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM representations").fetchone()[0] == 0
        assert db.execute("SELECT status FROM generation_runs").fetchone()[0] == "failed"


def test_inconsistent_embedding_dimensions_rejected(tmp_path):
    class Provider:
        def __init__(self, **kw): pass
        def embed(self, texts): return [[1.0] if i == 0 else [1.0, 0.0] for i, _ in enumerate(texts)]
    brain = Brain(tmp_path / "brain", embedding_provider_factory=Provider)
    paper = tmp_path / "paper.txt"
    paper.write_text("First evidence\n\nSecond evidence")
    doc = brain.ingest(paper)
    with pytest.raises(ValueError, match="dimensions"):
        brain.index_embeddings(doc.document_id, live=True)
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM representations").fetchone()[0] == 0
