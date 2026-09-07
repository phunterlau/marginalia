from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from research_brain import Brain
from research_brain.ingest import arxiv_identity, canonicalize_url, resolve_source
from research_brain.parsing import parse_arxiv_source, parse_html


PAPER_V1 = r"""# A useful paper

We use a paired contrast to isolate a nuisance variable.

## Objective

The regularizer controls policy drift:

$$
J(\theta) = E[r] - \beta KL(\pi_\theta || \pi_ref)
$$

This requires no gradient through the evaluator.
"""


class ResearchBrainCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paper = self.root / "paper.md"
        self.paper.write_text(PAPER_V1, encoding="utf-8")
        self.brain = Brain(self.root / "brain")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_and_fts5_are_initialized(self) -> None:
        with sqlite3.connect(self.brain.store.path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
            self.assertTrue({"source_assets", "documents", "document_versions", "document_blocks", "research_objects", "events", "block_fts", "object_fts"} <= tables)
            self.assertEqual(connection.execute("SELECT version FROM schema_migrations").fetchone()[0], 1)

    def test_ingestion_is_idempotent_and_preserves_equations(self) -> None:
        first = self.brain.ingest(self.paper)
        second = self.brain.ingest(self.paper)
        self.assertTrue(first.created_version)
        self.assertFalse(second.created_version)
        self.assertEqual(first.document_version_id, second.document_version_id)

        hits = self.brain.search("policy drift")
        self.assertTrue(any(hit.record_type == "document_block" for hit in hits))

        with self.brain.store.connect() as connection:
            equation = connection.execute(
                "SELECT raw_text, raw_latex FROM document_blocks WHERE document_version_id = ? AND block_type = 'equation'",
                (first.document_version_id,),
            ).fetchone()
            version_count = connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0]
        self.assertEqual(version_count, 1)
        self.assertIn(r"\beta KL", equation[0])
        self.assertEqual(equation[0], equation[1])

    def test_changed_source_creates_version_without_overwrite(self) -> None:
        first = self.brain.ingest(self.paper)
        self.paper.write_text(PAPER_V1 + "\nA later correction.\n", encoding="utf-8")
        second = self.brain.ingest(self.paper)
        self.assertNotEqual(first.document_version_id, second.document_version_id)
        self.assertEqual(first.document_id, second.document_id)
        document = self.brain.get_document(first.document_id)
        self.assertEqual(len(document["versions"]), 2)
        with self.brain.store.connect() as connection:
            assets = connection.execute(
                """SELECT a.local_path, a.sha256
                   FROM source_assets a
                   JOIN document_versions v ON v.source_asset_id = a.id
                   WHERE v.document_id = ?""",
                (first.document_id,),
            ).fetchall()
        self.assertEqual(len({row["sha256"] for row in assets}), 2)
        self.assertTrue(all(Path(row["local_path"]).exists() for row in assets))

    def test_object_epistemic_state_evidence_and_history(self) -> None:
        self.brain.ingest(self.paper)
        block_hit = next(hit for hit in self.brain.search("paired contrast") if hit.record_type == "document_block")
        card = self.brain.create_research_object(
            kind="method_card",
            title="Paired contrast",
            body="Subtract a matched observation to isolate a nuisance factor.",
            structured={"schema": "MethodCardV1", "gradients_required": False},
            origin="AGENT_EXTRACTED",
            review_state="UNREVIEWED",
            confidence=0.8,
            evidence=[(block_hit.record_id, "supports", 0.9)],
            actor="test-agent",
        )
        self.assertEqual(card.review_state, "UNREVIEWED")
        evidence = self.brain.get_evidence(block_hit.record_id)
        self.assertEqual(evidence["raw_text"], "We use a paired contrast to isolate a nuisance variable.")
        self.assertEqual(self.brain.get_history(card.id)[0]["event_type"], "object_created")
        object_hits = self.brain.search("matched nuisance", kinds=["method_card"])
        self.assertEqual(object_hits[0].record_id, card.id)

    def test_research_question_is_explicit_personal_memory(self) -> None:
        question = self.brain.create_research_question(
            "How can directions come from transformer structure?",
            constraints=["forward-only", "low compute"],
            available_access=["hidden activations"],
            desired_output="ranked perturbation basis",
        )
        self.assertEqual(question.origin, "USER_STATED")
        self.assertEqual(question.structured["schema"], "ResearchQuestionV1")
        self.assertIn("forward-only", question.structured["constraints"])

    def test_url_normalization_retains_arxiv_version_identity(self) -> None:
        self.assertEqual(canonicalize_url("HTTPS://WWW.Example.COM:443/a//b#fragment"), "https://www.example.com/a/b")
        self.assertEqual(arxiv_identity("https://arxiv.org/pdf/2305.18290v2.pdf"), ("2305.18290", "v2"))

    def test_html_math_keeps_document_order_context_and_tex(self) -> None:
        html = """<h2>Method</h2><p>Before <math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math> after.</p>"""
        blocks = parse_html(html)
        self.assertEqual([block.block_type for block in blocks], ["heading", "paragraph", "equation", "paragraph"])
        self.assertEqual(blocks[2].raw_latex, "x^2")
        self.assertEqual(blocks[2].section_path, "Method")
        self.assertEqual(blocks[1].raw_text, "Before")
        self.assertEqual(blocks[3].raw_text, "after.")

    def test_arxiv_source_bundle_follows_inputs_and_records_members(self) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            files = {
                "paper/main.tex": rb"\documentclass{article}\title{Source First}\begin{document}\input{method}\end{document}",
                "paper/method.tex": b"\\section{Method}\n\\begin{equation}\nx^2 + y^2\n\\end{equation}\n",
            }
            for name, contents in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        blocks = parse_arxiv_source(archive_bytes.getvalue())
        self.assertEqual(blocks[0].normalized_text, "Source First")
        equation = next(block for block in blocks if block.block_type == "equation")
        self.assertEqual(equation.metadata["source_member"], "paper/method.tex")
        self.assertIn("x^2 + y^2", equation.raw_latex)

    def test_arxiv_resolution_prefers_latest_source_and_falls_back_to_pdf(self) -> None:
        source_archive = io.BytesIO()
        with tarfile.open(fileobj=source_archive, mode="w:gz") as archive:
            contents = rb"\documentclass{article}\begin{document}Evidence\end{document}"
            info = tarfile.TarInfo("main.tex")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
        with patch("research_brain.ingest._arxiv_metadata", return_value=("v2", None)), patch("research_brain.ingest._download", return_value=(source_archive.getvalue(), "https://arxiv.org/src/2506.24056v2", "application/gzip")) as download:
            resolved = resolve_source("https://arxiv.org/abs/2506.24056")
        self.assertEqual(download.call_args.args[0], "https://arxiv.org/src/2506.24056v2")
        self.assertEqual(resolved.kind, "source_archive")

        def unavailable_then_pdf(url: str, *, timeout: float):
            if "/src/" in url:
                raise ValueError("no source")
            return b"%PDF-1.7", "https://arxiv.org/pdf/2506.24056v1.pdf", "application/pdf"

        with patch("research_brain.ingest._download", side_effect=unavailable_then_pdf) as download:
            resolved = resolve_source("https://arxiv.org/abs/2506.24056v1")
        self.assertEqual(download.call_args_list[0].args[0], "https://arxiv.org/src/2506.24056v1")
        self.assertEqual(download.call_args_list[1].args[0], "https://arxiv.org/pdf/2506.24056v1.pdf")
        self.assertEqual(resolved.kind, "pdf")
        self.assertEqual(resolved.version_label, "v1")

    def test_invalid_epistemic_values_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.brain.create_research_object(kind="hypothesis", body="x", origin="FACT")


if __name__ == "__main__":
    unittest.main()
