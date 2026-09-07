import json

import pytest

from research_brain import extraction


def block(id, text, section="s", type="paragraph", ordinal=0):
    return dict(id=id, raw_text=text, raw_latex=text if type == "equation" else None,
                block_type=type, section_path=section, ordinal=ordinal)


def test_prose_splits_deterministically_without_losing_characters(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_CHARS", 1000)
    raw = 'é😀"\\' * 1500
    blocks = [block("block_one", raw)]
    chunks = extraction._method_chunks(blocks)
    assert chunks == extraction._method_chunks(blocks)
    assert all(len(json.dumps(c, ensure_ascii=False)) <= 1000 for c in chunks)
    assert "".join(r["raw_text"] for c in chunks for r in c) == raw
    assert {r["block_id"] for c in chunks for r in c} == {"block_one"}


def test_sections_never_mixed_and_comments_not_sent():
    chunks = extraction._method_chunks([block("a", "prose", "a"), block("b", "other", "b"), block("c", "% ignored\n % ignored", "b")])
    assert len(chunks) == 2
    assert [r["block_id"] for c in chunks for r in c] == ["a", "b"]


def test_math_context_split_but_canonical_equation_unchanged(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_CHARS", 1200)
    blocks = [block("before", "p" * 4000, ordinal=0), block("eq", "$$ x=y $$", type="equation", ordinal=1), block("after", "q" * 4000, ordinal=2)]
    chunks = extraction._math_chunks(blocks)
    assert all(len(json.dumps(c, ensure_ascii=False)) <= 1200 for c in chunks)
    records = [r for c in chunks for r in c]
    assert {r["raw_latex"] for r in records} == {"$$ x=y $$"}
    contexts = [p for r in records for p in r["context"]]
    assert "".join(p["raw_text"] for p in contexts if p["block_id"] == "before") == "p" * 4000
    with pytest.raises(ValueError, match="Canonical equation"):
        extraction._math_chunks([block("huge", "x" * 2000, type="equation")])
