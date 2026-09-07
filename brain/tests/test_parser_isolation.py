import io
import tarfile
import time
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.ingest import ResolvedSource
from research_brain.parse_worker import compile_source
from research_brain.parsing import parse_arxiv_source, parse_text


def archive(members):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as tar:
        for name, text in members.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return result.getvalue()


def test_comments_do_not_include_unused_tex_and_sections_cross_includes():
    data = archive({"main.tex": "\\documentclass{article}\n\\begin{document}\n\\section{Method}\n% \\input{unused}\n\\input{part}\nAfter child.\n\\input{missing}\n\\end{document}",
                    "part.tex": "Inside child.\n\\input{main}", "unused.tex": "PRIVATE_COMMENT_CANARY"})
    diagnostics = {}
    blocks = parse_arxiv_source(data, diagnostics=diagnostics)
    assert diagnostics["main_tex"] == "main.tex"
    assert diagnostics["unused_tex_members"] == ["unused.tex"]
    assert diagnostics["recursive_includes"] == ["main.tex"]
    assert diagnostics["missing_includes"] == [{"source_member": "main.tex", "reference": "missing"}]
    assert all(b.section_path == "Method" for b in blocks if b.block_type == "paragraph")
    assert not any("PRIVATE_COMMENT_CANARY" in b.raw_text for b in blocks)


def test_repeated_include_has_distinct_blocks_and_same_source_span(tmp_path):
    data = archive({"main.tex": "\\documentclass{article}\n\\begin{document}\n\\input{part}\n\\input{part}\n\\end{document}", "part.tex": "Repeated evidence."})
    brain = Brain(tmp_path / "brain")
    source = ResolvedSource(data, "https://arxiv.org/src/2506.24056v2", "paper.tar.gz", "application/gzip", "source_archive", "https://arxiv.org/abs/2506.24056", "v2", {})
    result = brain.ingestor._ingest_resolved(source)
    blocks = brain.store.get_blocks(result.document_id)
    repeated = [b for b in blocks if b["raw_text"] == "Repeated evidence."]
    assert len(repeated) == 2
    assert repeated[0]["id"] != repeated[1]["id"]
    assert repeated[0]["char_start"] == repeated[1]["char_start"]
    assert repeated[0]["raw_sha256"] == repeated[1]["raw_sha256"]


def test_comment_only_text_is_archived_but_not_searchable(tmp_path):
    path = tmp_path / "paper.tex"
    path.write_text("\\section{Methods}\n% SEMANTIC_COMMENT_CANARY\nEvidence paragraph.\n")
    brain = Brain(tmp_path / "brain")
    result = brain.ingest(path)
    assert any(b["block_type"] == "comment" for b in brain.store.get_blocks(result.document_id))
    assert brain.search("SEMANTIC_COMMENT_CANARY") == []
    targets = brain.store.embedding_targets(result.document_id)
    assert not any("SEMANTIC_COMMENT_CANARY" in t["text"] for t in targets)


def test_crlf_spans_remain_exact():
    text = "First line\r\nSecond line\r\n\r\n$$\r\nx=y\r\n$$\r\n"
    for block in parse_text(text):
        assert text[block.metadata["char_start"]:block.metadata["char_end"]] == block.raw_text


def test_real_subprocess_deadline_reaps_parser():
    before = time.monotonic()
    with pytest.raises(RuntimeError, match="deadline"):
        compile_source(b"test", name="paper.txt", kind="text", timeout=0.001)
    assert time.monotonic() - before < 5


def test_parser_deadline_leaves_no_rows_or_assets(tmp_path):
    brain = Brain(tmp_path / "brain")
    paper = tmp_path / "paper.txt"
    paper.write_text("Some evidence")
    with patch("research_brain.ingest.compile_source", side_effect=RuntimeError("deadline")):
        with pytest.raises(RuntimeError):
            brain.ingest(paper)
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert list((brain.root / "assets").rglob("*")) == []


def test_parser_child_does_not_inherit_provider_credentials(monkeypatch):
    import subprocess
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    real_run = subprocess.run
    def checked_run(*args, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "OPENAI_BASE_URL" not in kwargs["env"]
        return real_run(*args, **kwargs)
    with patch("research_brain.parse_worker.subprocess.run", side_effect=checked_run):
        blocks, diagnostics = compile_source(b"Evidence", name="paper.txt", kind="text")
    assert blocks[0].raw_text == "Evidence"
    assert diagnostics["parser_deadline_seconds"] == 120


def test_repeated_include_expansion_is_bounded(monkeypatch):
    from research_brain import parsing
    monkeypatch.setattr(parsing, "MAX_INCLUDE_EXPANSIONS", 3)
    data = archive({"main.tex": "\\documentclass{article}\n\\begin{document}\n" + "\\input{part}\n" * 5 + "\\end{document}", "part.tex": "Evidence"})
    with pytest.raises(RuntimeError, match="expansion"):
        parse_arxiv_source(data)
