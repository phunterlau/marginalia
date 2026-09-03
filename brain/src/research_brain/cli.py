"""JSON-oriented command line interface for humans and agent clients."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
from typing import Any

from .brain import Brain
from .models import RetrievalFiltersV1


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    elif isinstance(value, list):
        value = [asdict(item) if is_dataclass(item) else item for item in value]
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _json_object(value: str, *, label: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _json_list(value: str, *, label: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{label} must be a JSON list of strings")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research", description="Evidence-first research ledger")
    parser.add_argument("--root", default="data", help="Brain data directory (default: ./data)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create/migrate the Brain database")

    ingest = commands.add_parser("ingest", help="Archive and structurally index a file or URL")
    ingest.add_argument("source")

    search = commands.add_parser("search", help="Search evidence blocks and research objects")
    search.add_argument("query")
    search.add_argument("--kind", action="append", dest="kinds")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--filters", default="{}", help="RetrievalFiltersV1 JSON object")
    search.add_argument("--semantic-live", action="store_true", help="Spend one embedding query call")

    recall = commands.add_parser("recall", help="Reliable recall: source evidence and accepted cards")
    recall.add_argument("query")
    recall.add_argument("--kind", action="append", dest="kinds")
    recall.add_argument("--limit", type=int, default=10)
    recall.add_argument("--filters", default="{}")
    recall.add_argument("--semantic-live", action="store_true", help="Spend one embedding query call")

    evidence = commands.add_parser("evidence", help="Show a block with complete source locator")
    evidence.add_argument("block_id")

    paper = commands.add_parser("paper", help="Document operations")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    paper_show = paper_commands.add_parser("show")
    paper_show.add_argument("document_id")

    question = commands.add_parser("question", help="Research-question operations")
    question_commands = question.add_subparsers(dest="question_command", required=True)
    question_add = question_commands.add_parser("add")
    question_add.add_argument("question")
    question_add.add_argument("--title")
    question_add.add_argument("--constraints", default="[]")
    question_add.add_argument("--available-access", default="[]")
    question_add.add_argument("--desired-output")

    object_parser = commands.add_parser("object", help="Generic research-object operations")
    object_commands = object_parser.add_subparsers(dest="object_command", required=True)
    object_add = object_commands.add_parser("add")
    object_add.add_argument("kind")
    object_add.add_argument("body")
    object_add.add_argument("--title")
    object_add.add_argument("--structured", default="{}")
    object_add.add_argument("--origin", default="USER_STATED")
    object_add.add_argument("--review-state", default="ACCEPTED")
    object_add.add_argument("--confidence", type=float)
    object_show = object_commands.add_parser("show")
    object_show.add_argument("object_id")
    object_evidence = object_commands.add_parser("evidence")
    object_evidence.add_argument("object_id")
    object_review = object_commands.add_parser("review")
    object_review.add_argument("object_id")
    review = object_review.add_mutually_exclusive_group(required=True)
    review.add_argument("--accept", action="store_true")
    review.add_argument("--reject", action="store_true")
    review.add_argument("--dispute", action="store_true")
    object_review.add_argument("--note")

    extract = commands.add_parser("extract", help="Plan or execute evidence-constrained extraction")
    extract_commands = extract.add_subparsers(dest="extract_task", required=True)
    for task in ("methods", "math"):
        command = extract_commands.add_parser(task)
        command.add_argument("document_id")
        command.add_argument("--compilation")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--live", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        command.add_argument("--force", action="store_true")

    index = commands.add_parser("index", help="Derived representation indexes")
    index_commands = index.add_subparsers(dest="index_task", required=True)
    embeddings = index_commands.add_parser("embeddings")
    embeddings.add_argument("target", help="document id or 'all'")
    mode = embeddings.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    embeddings.add_argument("--force", action="store_true")

    corpus = commands.add_parser("corpus", help="Pinned offline corpus operations")
    corpus_commands = corpus.add_subparsers(dest="corpus_task", required=True)
    corpus_load = corpus_commands.add_parser("load")
    corpus_load.add_argument("manifest_dir")

    evaluate_parser = commands.add_parser("evaluate", help="Run a retrieval evaluation specification")
    evaluate_parser.add_argument("spec")
    evaluate_parser.add_argument("--semantic-live", action="store_true")

    history = commands.add_parser("history", help="Show append-only history for an object")
    history.add_argument("object_id")
    return parser


def run(args: argparse.Namespace) -> Any:
    brain = Brain(Path(args.root))
    if args.command == "init":
        return {"database": str(brain.store.path), "status": "ready"}
    if args.command == "ingest":
        return brain.ingest(args.source)
    if args.command == "search":
        return brain.search(args.query, kinds=args.kinds,
                            filters=RetrievalFiltersV1(**_json_object(args.filters, label="filters")),
                            limit=args.limit, semantic_live=args.semantic_live)
    if args.command == "recall":
        return brain.recall(args.query, kinds=args.kinds,
                            filters=RetrievalFiltersV1(**_json_object(args.filters, label="filters")),
                            limit=args.limit, semantic_live=args.semantic_live)
    if args.command == "evidence":
        result = brain.get_evidence(args.block_id)
        if result is None:
            raise LookupError(f"Evidence block not found: {args.block_id}")
        return result
    if args.command == "paper" and args.paper_command == "show":
        result = brain.get_document(args.document_id)
        if result is None:
            raise LookupError(f"Document not found: {args.document_id}")
        return result
    if args.command == "question" and args.question_command == "add":
        return brain.create_research_question(
            args.question,
            title=args.title,
            constraints=_json_list(args.constraints, label="constraints"),
            available_access=_json_list(args.available_access, label="available-access"),
            desired_output=args.desired_output,
        )
    if args.command == "object" and args.object_command == "add":
        return brain.create_research_object(
            kind=args.kind, body=args.body, title=args.title,
            structured=_json_object(args.structured, label="structured"), origin=args.origin,
            review_state=args.review_state, confidence=args.confidence,
        )
    if args.command == "object" and args.object_command in {"show", "evidence"}:
        result = brain.get_research_object(args.object_id)
        if result is None:
            raise LookupError(f"Research object not found: {args.object_id}")
        return result if args.object_command == "evidence" else {key: value for key, value in result.items() if key != "evidence"}
    if args.command == "object" and args.object_command == "review":
        state = "ACCEPTED" if args.accept else "REJECTED" if args.reject else "DISPUTED"
        return brain.review_research_object(args.object_id, review_state=state, note=args.note)
    if args.command == "extract":
        return brain.extract(args.extract_task, args.document_id, compilation_id=args.compilation,
                             live=args.live, force=args.force)
    if args.command == "index" and args.index_task == "embeddings":
        return brain.index_embeddings(args.target, live=args.live, force=args.force)
    if args.command == "corpus" and args.corpus_task == "load":
        manifests = sorted(Path(args.manifest_dir).glob("*.json"))
        if not manifests:
            raise LookupError(f"No fixture manifests found: {args.manifest_dir}")
        return [brain.ingest_manifest(path) for path in manifests]
    if args.command == "evaluate":
        from .evaluation import evaluate
        return evaluate(brain, args.spec, semantic_live=args.semantic_live)
    if args.command == "history":
        return brain.get_history(args.object_id)
    raise AssertionError("unhandled command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        result = run(parser.parse_args(argv))
    except Exception as exc:
        # The CLI is a JSON tool boundary for agents; provider exceptions must not
        # turn into an unstructured traceback on stdout/stderr.
        print(_json({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
