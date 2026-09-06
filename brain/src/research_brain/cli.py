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
    reviewer = commands.add_parser("review-ui", help="Open the local memory reviewer")
    reviewer.add_argument("--port", type=int, default=8765)

    ingest = commands.add_parser("ingest", help="Archive and structurally index a file or URL")
    ingest.add_argument("source")

    search = commands.add_parser("search", help="Search evidence blocks and research objects")
    search.add_argument("query")
    search.add_argument("--kind", action="append", dest="kinds")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--filters", default="{}", help="RetrievalFiltersV1 JSON object")
    search.add_argument("--semantic-live", action="store_true", help="Allow one embedding query call on cache miss")

    recall = commands.add_parser("recall", help="Reliable recall: source evidence and accepted cards")
    recall.add_argument("query")
    recall.add_argument("--kind", action="append", dest="kinds")
    recall.add_argument("--limit", type=int, default=10)
    recall.add_argument("--filters", default="{}")
    recall.add_argument("--semantic-live", action="store_true", help="Allow one embedding query call on cache miss")

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
    question_add.add_argument("--thread")
    question_link = question_commands.add_parser("link")
    question_link.add_argument("source_question_id")
    question_link.add_argument("relation")
    question_link.add_argument("target_object_id")
    question_link.add_argument("--metadata", default="{}")
    question_genealogy = question_commands.add_parser("genealogy")
    question_genealogy.add_argument("question_id")

    thread = commands.add_parser("thread", help="Hot Frontier thread operations")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_add = thread_commands.add_parser("add")
    thread_add.add_argument("title")
    thread_add.add_argument("--goal", required=True)
    thread_add.add_argument("--status", default="active")
    thread_add.add_argument("--known", default="[]")
    thread_add.add_argument("--unknown", default="[]")
    thread_add.add_argument("--constraints", default="[]")
    thread_add.add_argument("--pending-decisions", default="[]")
    thread_add.add_argument("--pending-experiments", default="[]")
    thread_show = thread_commands.add_parser("show")
    thread_show.add_argument("thread_id")
    thread_update = thread_commands.add_parser("update")
    thread_update.add_argument("thread_id")
    thread_update.add_argument("--changes", required=True, help="ResearchThreadV1 field updates as JSON")
    thread_snapshot = thread_commands.add_parser("snapshot")
    thread_snapshot.add_argument("thread_id")
    thread_snapshot.add_argument("--latest", action="store_true")

    hypothesis = commands.add_parser("hypothesis", help="Hypothesis operations")
    hypothesis_commands = hypothesis.add_subparsers(dest="hypothesis_command", required=True)
    hypothesis_add = hypothesis_commands.add_parser("add")
    hypothesis_add.add_argument("statement")
    hypothesis_add.add_argument("--thread", required=True)
    hypothesis_add.add_argument("--status", default="active")
    hypothesis_add.add_argument("--evidence-for", default="[]")
    hypothesis_add.add_argument("--evidence-against", default="[]")
    hypothesis_add.add_argument("--critical-unknowns", default="[]")
    hypothesis_add.add_argument("--what-would-strengthen", default="[]")
    hypothesis_add.add_argument("--what-would-weaken", default="[]")
    hypothesis_add.add_argument("--killer-test")

    transfer = commands.add_parser("transfer", help="Explicit cross-domain transfer hypotheses")
    transfer_commands = transfer.add_subparsers(dest="transfer_command", required=True)
    transfer_add = transfer_commands.add_parser("add")
    transfer_add.add_argument("target_question_id")
    transfer_add.add_argument("source_object_id")
    transfer_add.add_argument("--mapping", required=True)
    transfer_add.add_argument("--why-promising", required=True)
    transfer_add.add_argument("--mismatches", required=True)
    transfer_add.add_argument("--proposed-test", required=True)
    transfer_add.add_argument("--status", default="proposed")
    transfer_add.add_argument("--thread")

    observe = commands.add_parser("observe", help="Record an evidence-backed observation")
    observe.add_argument("statement")
    observe.add_argument("--thread", required=True)
    observe.add_argument("--conditions", required=True)
    observe.add_argument("--evidence-refs", required=True)

    interpret = commands.add_parser("interpret", help="Record an interpretation of observations")
    interpret.add_argument("statement")
    interpret.add_argument("--thread", required=True)
    interpret.add_argument("--derived-from", required=True)

    tension = commands.add_parser("tension", help="Unresolved frontier-tension operations")
    tension_commands = tension.add_subparsers(dest="tension_command", required=True)
    tension_add = tension_commands.add_parser("add")
    tension_add.add_argument("statement")
    tension_add.add_argument("--thread", required=True)
    tension_add.add_argument("--side-a", required=True)
    tension_add.add_argument("--side-b", required=True)
    tension_add.add_argument("--possible-explanations", default="[]")

    usage = commands.add_parser("usage", help="Record negative or historical method use")
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_add = usage_commands.add_parser("add")
    usage_add.add_argument("candidate")
    usage_add.add_argument("--thread", required=True)
    usage_add.add_argument("--disposition", required=True)
    usage_add.add_argument("--reason", required=True)
    usage_add.add_argument("--what-would-reconsider", required=True)
    usage_add.add_argument("--body")

    context = commands.add_parser("context", help="Compile a task-specific ResearchPacket")
    context.add_argument("question")
    context.add_argument("--thread")
    context.add_argument("--mode", choices=("recall", "analysis", "critique", "brainstorm", "decision"),
                         default="analysis")
    context.add_argument("--filters", default="{}")
    context.add_argument("--limit", type=int, default=8)
    context.add_argument("--blind-first")
    context.add_argument("--semantic-live", action="store_true")

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
    object_list = object_commands.add_parser("list")
    object_list.add_argument("--kind", action="append", dest="kinds")
    object_list.add_argument("--origin", action="append", dest="origins")
    object_list.add_argument("--review-state", action="append", dest="review_states")
    object_list.add_argument("--document")
    object_list.add_argument("--latest-extraction-only", action="store_true")
    object_list.add_argument("--limit", type=int, default=50)
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
    corpus_benchmark = corpus_commands.add_parser("benchmark")
    corpus_benchmark.add_argument("spec")
    corpus_benchmark.add_argument("--iterations", type=int, default=25)
    corpus_benchmark.add_argument(
        "--semantic-live", action="store_true",
        help="Allow one embedding call per uncached benchmark query before offline timing",
    )

    evaluate_parser = commands.add_parser("evaluate", help="Run a retrieval evaluation specification")
    evaluate_parser.add_argument("spec")
    evaluate_parser.add_argument("--semantic-live", action="store_true")
    frontier_eval = commands.add_parser('evaluate-frontier', help='Offline ResearchPacket acceptance checks')
    frontier_eval.add_argument('spec')
    frontier_eval.add_argument('--output', help='Save a new JSON report; never overwrite an existing artifact')
    comparison = commands.add_parser('compare-research', help='Preview or run isolated judged research comparisons')
    comparison.add_argument('spec')
    comparison_target = comparison.add_mutually_exclusive_group(required=True)
    comparison_target.add_argument('--fixture', action='store_true')
    comparison_target.add_argument('--thread')
    comparison_spend = comparison.add_mutually_exclusive_group()
    comparison_spend.add_argument('--live', action='store_true')
    comparison_spend.add_argument('--dry-run', action='store_true')
    comparison.add_argument('--output-dir', type=Path)
    target = frontier_eval.add_mutually_exclusive_group(required=True)
    target.add_argument('--fixture', action='store_true', help='Use an isolated synthetic frontier; ignore --root')
    target.add_argument('--thread', help='Evaluate an existing thread without provider calls')

    history = commands.add_parser("history", help="Show append-only history for an object or thread")
    history_target = history.add_mutually_exclusive_group(required=True)
    history_target.add_argument("object_id", nargs="?")
    history_target.add_argument("--thread", dest="thread_id")
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.command == 'compare-research':
        from .comparison import compare_research, load_comparison_spec
        load_comparison_spec(args.spec)
        options = dict(live=args.live, output_dir=args.output_dir)
        if args.fixture:
            from tempfile import TemporaryDirectory
            from .frontier_evaluation import seed_frontier_fixture
            with TemporaryDirectory(prefix='research-comparison-') as temporary:
                fixture_brain = Brain(Path(temporary))
                thread = seed_frontier_fixture(fixture_brain)
                return compare_research(fixture_brain,args.spec,thread_id=thread,fixture=True,**options)
        from .store import SQLiteStore
        SQLiteStore(Path(args.root).expanduser().resolve() / 'brain.sqlite3', initialize=False)
        return compare_research(Brain(Path(args.root)),args.spec,thread_id=args.thread,**options)
    if args.command == 'evaluate-frontier':
        from .frontier_evaluation import evaluate_frontier, load_spec, seed_frontier_fixture
        load_spec(args.spec)
        def finish(report):
            if args.output:
                output = Path(args.output).expanduser()
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open('x', encoding='utf-8') as stream:
                    stream.write(_json(report) + '\n')
            return report
        if args.fixture:
            from tempfile import TemporaryDirectory
            with TemporaryDirectory(prefix='research-frontier-eval-') as temporary:
                fixture_brain = Brain(Path(temporary))
                thread = seed_frontier_fixture(fixture_brain)
                return finish(evaluate_frontier(fixture_brain, args.spec, thread_id=thread, fixture='synthetic-frontier-v1'))
        # Fail before Brain can initialize or migrate a missing/incompatible database.
        from .store import SQLiteStore
        SQLiteStore(Path(args.root).expanduser().resolve() / 'brain.sqlite3', initialize=False)
        return finish(evaluate_frontier(Brain(Path(args.root)), args.spec, thread_id=args.thread))
    if args.command == "review-ui":
        from .reviewer import serve
        return serve(Path(args.root), args.port)
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
            thread_id=args.thread,
        )
    if args.command == "question" and args.question_command == "link":
        return brain.link_question(
            args.source_question_id, args.relation, args.target_object_id,
            metadata=_json_object(args.metadata, label="metadata"),
        )
    if args.command == "question" and args.question_command == "genealogy":
        return brain.get_question_genealogy(args.question_id)
    if args.command == "thread" and args.thread_command == "add":
        return brain.create_thread(
            args.title, goal=args.goal, status=args.status,
            known=_json_list(args.known, label="known"),
            unknown=_json_list(args.unknown, label="unknown"),
            constraints=_json_list(args.constraints, label="constraints"),
            pending_decisions=_json_list(args.pending_decisions, label="pending-decisions"),
            pending_experiments=_json_list(args.pending_experiments, label="pending-experiments"),
        )
    if args.command == "thread" and args.thread_command == "show":
        result = brain.get_thread(args.thread_id)
        if result is None:
            raise LookupError(f"Research thread not found: {args.thread_id}")
        return result
    if args.command == "thread" and args.thread_command == "update":
        return brain.update_frontier(args.thread_id, _json_object(args.changes, label="changes"))
    if args.command == "thread" and args.thread_command == "snapshot":
        if args.latest:
            result = brain.get_latest_frontier_snapshot(args.thread_id)
            if result is None:
                raise LookupError(f"No frontier snapshot found for: {args.thread_id}")
            return result
        return brain.create_frontier_snapshot(args.thread_id)
    if args.command == "hypothesis" and args.hypothesis_command == "add":
        return brain.create_hypothesis(
            args.statement, thread_id=args.thread, status=args.status,
            evidence_for=_json_list(args.evidence_for, label="evidence-for"),
            evidence_against=_json_list(args.evidence_against, label="evidence-against"),
            critical_unknowns=_json_list(args.critical_unknowns, label="critical-unknowns"),
            what_would_strengthen=_json_list(args.what_would_strengthen, label="what-would-strengthen"),
            what_would_weaken=_json_list(args.what_would_weaken, label="what-would-weaken"),
            killer_test=args.killer_test,
        )
    if args.command == "transfer" and args.transfer_command == "add":
        return brain.create_transfer_hypothesis(
            target_question_id=args.target_question_id,
            source_object_id=args.source_object_id,
            mapping_claims=_json_object(args.mapping, label="mapping"),
            why_promising=_json_list(args.why_promising, label="why-promising"),
            mismatches=_json_list(args.mismatches, label="mismatches"),
            proposed_test=args.proposed_test,
            status=args.status,
            thread_id=args.thread,
        )
    if args.command == "observe":
        return brain.record_observation(
            args.statement, thread_id=args.thread,
            conditions=_json_object(args.conditions, label="conditions"),
            evidence_refs=_json_list(args.evidence_refs, label="evidence-refs"),
        )
    if args.command == "interpret":
        return brain.record_interpretation(
            args.statement, thread_id=args.thread,
            derived_from=_json_list(args.derived_from, label="derived-from"),
        )
    if args.command == "tension" and args.tension_command == "add":
        return brain.record_tension(
            args.statement, thread_id=args.thread,
            side_a=_json_list(args.side_a, label="side-a"),
            side_b=_json_list(args.side_b, label="side-b"),
            possible_explanations=_json_list(args.possible_explanations, label="possible-explanations"),
        )
    if args.command == "usage" and args.usage_command == "add":
        return brain.record_usage_episode(
            args.body or args.reason, candidate=args.candidate, disposition=args.disposition,
            reason=args.reason, what_would_reconsider=args.what_would_reconsider,
            thread_id=args.thread,
        )
    if args.command == "context":
        return brain.context(
            args.question, thread_id=args.thread, mode=args.mode,
            filters=RetrievalFiltersV1(**_json_object(args.filters, label="filters")),
            limit=args.limit, blind_first=args.blind_first, semantic_live=args.semantic_live,
        )
    if args.command == "object" and args.object_command == "add":
        return brain.create_research_object(
            kind=args.kind, body=args.body, title=args.title,
            structured=_json_object(args.structured, label="structured"), origin=args.origin,
            review_state=args.review_state, confidence=args.confidence,
        )
    if args.command == "object" and args.object_command == "list":
        return brain.list_research_objects(
            kinds=args.kinds,
            origins=args.origins,
            review_states=args.review_states,
            document_id=args.document,
            latest_extraction_only=args.latest_extraction_only,
            limit=args.limit,
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
    if args.command == "corpus" and args.corpus_task == "benchmark":
        from .benchmark import benchmark_corpus_queries
        return benchmark_corpus_queries(
            brain, args.spec, iterations=args.iterations, semantic_live=args.semantic_live,
        )
    if args.command == "evaluate":
        from .evaluation import evaluate
        return evaluate(brain, args.spec, semantic_live=args.semantic_live)
    if args.command == "history":
        return brain.get_history(args.thread_id or args.object_id)
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
    return 1 if is_dataclass(result) and getattr(result, "passed", True) is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
