from dataclasses import replace
import gzip
import io
import tarfile
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.ingest import ResolvedSource, arxiv_identity, resolve_source
from research_brain.parsing import _safe_tex_members


def test_strict_arxiv_identity():
    for url in ("https://evil.test/arxiv.org/abs/2506.24056", "https://arxiv.org/abs/2506.24056oops", "https://arxiv.org/abs/2506.24056v0", "https://user@arxiv.org/abs/2506.24056"):
        assert arxiv_identity(url) is None
    assert arxiv_identity("https://arxiv.org/src/2506.24056v2") == ("2506.24056", "v2")
    assert arxiv_identity("https://arxiv.org/abs/hep-th/9901001v1") == ("hep-th/9901001", "v1")


def test_unknown_revision_fails_before_source_download():
    with patch("research_brain.ingest._arxiv_metadata", return_value=(None, None)), patch("research_brain.ingest._download") as download:
        with pytest.raises(ValueError, match="resolve"):
            resolve_source("https://arxiv.org/abs/2506.24056")
        download.assert_not_called()


def test_unused_members_and_gzip_bombs_are_bounded():
    with pytest.raises(RuntimeError, match="decompressed"):
        _safe_tex_members(gzip.compress(b"x" * 10000), max_total_bytes=100)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for index in range(4):
            info = tarfile.TarInfo(f"unused{index}.png")
            tar.addfile(info)
    with pytest.raises(RuntimeError, match="member limit"):
        _safe_tex_members(archive.getvalue(), max_members=2)


def test_identical_bytes_have_distinct_revision_identity(tmp_path):
    brain = Brain(tmp_path)
    source = ResolvedSource(b"\\section{Test}\nSome evidence.", "https://arxiv.org/src/2506.24056v1", "main.tex", "application/x-tex", "tex", "https://arxiv.org/abs/2506.24056", "v1", {"arxiv": "2506.24056"})
    first = brain.ingestor._ingest_resolved(source)
    second = brain.ingestor._ingest_resolved(replace(source, version_label="v2", uri="https://arxiv.org/src/2506.24056v2"))
    assert first.document_version_id != second.document_version_id
    assert brain.ingestor._ingest_resolved(source).document_version_id == first.document_version_id


def test_failed_parse_does_not_promote_asset(tmp_path):
    brain = Brain(tmp_path)
    source = ResolvedSource(b"bad archive", "https://arxiv.org/src/2506.24056v1", "paper.tar.gz", "application/gzip", "source_archive", "https://arxiv.org/abs/2506.24056", "v1", {})
    with pytest.raises(ValueError):
        brain.ingestor._ingest_resolved(source)
    assert list((tmp_path / "assets").rglob("*")) == []
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0] == 0
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
