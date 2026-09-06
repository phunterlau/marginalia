"""A bounded Brain-backed research loop; all writes stay in a new local run."""
from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3

from .cli import file_hash, verify, write_json
from .core import digest
from .development import run_development, validate_development


def load(path):
    return json.loads(path.read_text())


def inventory(root):
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p != root / "research-checksums.json"}


def verify_research(root):
    if load(root / "research-checksums.json") != inventory(root):
        raise ValueError("Research artifact integrity check failed")
    return load(root / "research-status.json")


def disjoint(output, inputs):
    output = output.resolve()
    for source in inputs:
        source = source.resolve()
        if output == source or output in source.parents or source in output.parents:
            raise ValueError("Output must be separate from source data and pilot")


def snapshot_brain(source, destination):
    """SQLite online backup from a read-only handle; assets copied, never linked."""
    from research_brain.store import SQLiteStore
    SQLiteStore(source / "brain.sqlite3", initialize=False)
    destination.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect((source / "brain.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination / "brain.sqlite3")) as dst:
            src.backup(dst)
    if (source / "assets").exists():
        shutil.copytree(source / "assets", destination / "assets")


def _object(brain, kind, body, structured, *, origin="AGENT_PROPOSED", review_state="UNREVIEWED"):
    # Explicit origin avoids falsely attributing generated research choices to the user.
    return brain.create_research_object(kind=kind, title=body[:120], body=body,
        structured=structured, origin=origin, review_state=review_state, actor="interpretability-pipeline")


def collect_literature(brain):
    queries = ["activation steering", "contrastive activation", "representation direction"]
    result, evidence = [], {}
    for query in queries:
        hits = brain.retriever.retrieve(query, limit=4, reliable=True)
        result.append({"query": query, "lane": "reliable", "hits": [asdict(h) for h in hits]})
        for hit in hits:
            ids = [e["block_id"] for e in hit.evidence if e.get("block_id")]
            if hit.record_id.startswith("block_"):
                ids.append(hit.record_id)
            for block_id in ids:
                block = brain.get_evidence(block_id)
                if block is None:
                    raise ValueError(f"Missing literature evidence: {block_id}")
                evidence[block_id] = block
    return {"queries": result, "evidence": evidence,
            "boundary": "Related source methods, not evidence that this pilot works or a paper reproduction."}


def choose_next(packet, result_id):
    """Predeclared example policy reads the retrieved result, not hidden run state."""
    buckets = ("relevant_memory", "historical_attempts", "counterevidence", "tensions")
    items = [item for bucket in buckets for item in packet[bucket]]
    result = next((item for item in items if item["record_id"] == result_id), None)
    if result is None or "few_shot_qualified" not in result["structured"]:
        return {"action": "inspect_missing_result", "basis_ids": [],
                "reason": "The packet did not retrieve the diagnostic result; do not infer its outcome."}
    qualified = result["structured"]["few_shot_qualified"]
    if type(qualified) is not bool:
        raise ValueError("Invalid retrieved qualification flag")
    return {"action": "freeze_fresh_steering_holdout" if qualified else "diagnose_label_readout",
            "basis_ids": [result_id], "policy": "baseline-gate/v1",
            "reason": ("Both few-shot orders passed the frozen development gate. Freeze new topics/templates before another intervention study."
                       if qualified else "The fixed baseline repair did not pass both orders. Inspect label/readout suitability before more steering."),
            "scientific_status": "AGENT_PROPOSED / UNREVIEWED; no claim of generalization"}


def choose_diagnostic(packet, observation_id):
    observation = next((item for item in packet["counterevidence"]
                        if item["record_id"] == observation_id), None)
    accuracy = (observation or {}).get("structured", {}).get("conditions", {}).get("baseline_label_accuracy")
    if type(accuracy) not in (float, int) or not 0 <= accuracy < 0.8:
        raise ValueError("Packet does not establish a weak pilot baseline; this diagnostic policy does not apply")
    return {"action": "run_frozen_baseline_diagnostic", "basis_ids": [observation_id],
            "policy": "weak-baseline-first/v1", "threshold": 0.8,
            "reason": "Retrieved pilot accuracy is below the example policy threshold; diagnose the readout before tuning interventions."}


def run_research(source, pilot_dir, protocol, output, cache_dir, *, live=False):
    from research_brain import Brain
    from research_brain.frontier import thread_payload
    source, pilot_dir, output = map(lambda p: Path(p).resolve(), (source, pilot_dir, output))
    disjoint(output, [source, pilot_dir])
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if verify(pilot_dir)["status"] != "complete":
        raise ValueError("A complete, verified pilot is required")
    pilot = load(pilot_dir / "config.json")
    validate_development(protocol, pilot)
    from research_brain.store import SQLiteStore
    SQLiteStore(source / "brain.sqlite3", initialize=False)
    if not live:
        return {"mode": "dry-run", "writes": 0, "model_calls": 0,
                "planned_compute": protocol["budget"], "output": str(output),
                "protocol_digest": digest(protocol)}
    source_before = inventory(source)
    pilot_before = inventory(pilot_dir)
    output.mkdir(parents=True)
    try:
        snapshot_brain(source, output / "brain")
        # Keep original measurements inside the example; never rewrite their manifests.
        shutil.copytree(pilot_dir, output / "pilot")
        write_json(output / "protocol.json", protocol)
        import research_brain
        brain_src = Path(research_brain.__file__).parent
        write_json(output / "brain-implementation.json", {
            str(p.relative_to(brain_src)): p.read_text() for p in sorted(brain_src.rglob("*.py"))})
        write_json(output / "manifest.json", {"schema": "BrainResearchExampleV1",
            "started_at": datetime.now(timezone.utc).isoformat(), "source_root": str(source),
            "source_inventory_digest": digest(source_before), "pilot_inventory_digest": digest(pilot_before),
            "protocol_digest": digest(protocol), "model_revision": pilot["revision"],
            "driver": "bounded deterministic policy plus agent-authored hypotheses; no LLM synthesis"})
        def forbidden_provider(*args, **kwargs):
            raise AssertionError("Providers are forbidden in this offline workflow")
        brain = Brain(output / "brain", extraction_provider_factory=forbidden_provider,
                      embedding_provider_factory=forbidden_provider)
        constraints = ["No gradients or training", "Local pinned model; no provider calls",
                       "Development data cannot become a fresh steering evaluation", "Interpretations need human review"]
        thread = _object(brain, "research_thread", "Diagnose sentiment activation steering before interpreting direction effects",
            thread_payload(goal="Test whether forward-only activation directions support a meaningful sentiment readout",
                constraints=constraints, unknown=["Is the pilot label readout suitable?"],
                pending_experiments=[protocol["question"]]))
        question = _object(brain, "research_question", protocol["question"], {
            "schema": "ResearchQuestionV1", "question": protocol["question"], "thread_id": thread.id,
            "status": "open", "constraints": constraints})
        literature = collect_literature(brain)
        write_json(output / "literature.json", literature)
        stats = load(pilot_dir / "observations.json")
        accuracy = stats["baseline_label_accuracy"]
        pilot_result = _object(brain, "experiment_result", f"Archived sentiment intervention pilot: baseline accuracy {accuracy:.1%}", {
            "schema": "ExperimentResultV1", "thread_id": thread.id,
            "artifact": "pilot/observations.json", "sha256": file_hash(output / "pilot/observations.json"),
            "baseline_label_accuracy": accuracy, "conditions": stats["conditions"],
            "revision": pilot["revision"]}, origin="EXPERIMENT_OBSERVED", review_state="ACCEPTED")
        observed = brain.record_observation(
            f"Sentiment pilot baseline two-label accuracy was {accuracy:.1%}; this is a readout diagnostic, not task validation.",
            thread_id=thread.id, conditions={"n": 2*len(pilot["evaluation"]), "config": "pilot/config.json",
                                            "baseline_label_accuracy": accuracy},
            evidence_refs=[pilot_result.id])
        hypothesis = brain.create_hypothesis("Few-shot label demonstrations may repair the weak sentiment readout without changing weights.",
            thread_id=thread.id, evidence_for=[observed.id],
            critical_unknowns=["Baseline failure could involve token case, formatting, or the task itself; few-shot format changes multiple factors."],
            killer_test=protocol["selection_rule"], origin="AGENT_PROPOSED", review_state="UNREVIEWED")
        history = _object(brain, "usage_episode", "Do not promote pilot activation gap shifts to useful sentiment steering.", {
            "schema": "UsageEpisodeV1", "thread_id": thread.id, "candidate": "pilot as behavioral validation",
            "disposition": "insufficient_evidence", "reason": "Weak original task baseline; only a small one-token statistic.",
            "what_would_reconsider": "Qualified development baseline followed by a new independent intervention holdout."})
        brain.link_question(question.id, "MOTIVATED_BY", observed.id, origin="AGENT_PROPOSED", review_state="UNREVIEWED")
        before = asdict(brain.context_compiler.compile("sentiment pilot baseline readout observations failed validation", thread_id=thread.id, mode="critique", limit=10))
        write_json(output / "packet-before.json", before)
        selection = choose_diagnostic(before, observed.id)
        write_json(output / "diagnostic-selection.json", selection)
        frozen = _object(brain, "experiment", protocol["question"], {
            "schema": "ExperimentV1", "thread_id": thread.id, "protocol_digest": digest(protocol),
            "protocol_artifact": "protocol.json", "basis_ids": [observed.id, hypothesis.id, history.id],
            "status": "frozen", "budget": protocol["budget"]})
        # Protocol and Brain provenance are persisted before any new model measurement.
        result = run_development(protocol, pilot, output / "development", cache_dir)
        if load(output / "protocol.json") != protocol:
            raise ValueError("Frozen protocol changed during execution")
        diagnostic = _object(brain, "experiment_result", "Sentiment baseline development diagnostic result", {
            "schema": "ExperimentResultV1", "thread_id": thread.id, "experiment_id": frozen.id,
            "artifact": "development/observations.json", "sha256": file_hash(output / "development/observations.json"),
            **result}, origin="EXPERIMENT_OBSERVED", review_state="ACCEPTED")
        summary = "; ".join(f"{c['template']}: {c['correct']}/{c['n']}, target mass {c['mean_target_probability_mass']:.3f}, gate {c['gate_passed']}"
                            for c in result["conditions"])
        new_observation = brain.record_observation("Sentiment baseline development diagnostic: " + summary,
            thread_id=thread.id, conditions={"protocol_digest": digest(protocol), "scope": "development_only"},
            evidence_refs=[diagnostic.id])
        interpretation = brain.record_interpretation(
            "The fixed few-shot format qualified on development only; transfer and useful steering remain untested."
            if result["few_shot_qualified"] else "The tested format repair did not qualify; the sentiment readout needs further diagnosis before steering claims.",
            thread_id=thread.id, derived_from=[new_observation.id])
        after = asdict(brain.context_compiler.compile("sentiment baseline development diagnostic result", thread_id=thread.id, mode="analysis", limit=10))
        write_json(output / "packet-after.json", after)
        next_step = choose_next(after, diagnostic.id)
        write_json(output / "next-step.json", next_step)
        decision = _object(brain, "decision", next_step["reason"], {"schema": "DecisionV1", "thread_id": thread.id, **next_step})
        brain.update_frontier(thread.id, {"known": [summary], "pending_experiments": [next_step["reason"]],
            "unknown": ["Does a fresh intervention holdout support useful sentiment steering?", "Do effects transfer across templates and seeds?"]},
            actor="interpretability-pipeline")
        snapshot = brain.create_frontier_snapshot(thread.id)
        records = brain.store.list_object_records(thread_id=thread.id)
        write_json(output / "thread.json", {"thread": brain.get_thread(thread.id), "records": records,
            "history": {r["id"]: brain.get_history(r["id"]) for r in [brain.get_thread(thread.id), *records]}})
        ids = {"thread": thread.id, "question": question.id, "hypothesis": hypothesis.id,
               "pilot_result": pilot_result.id, "pilot_observation": observed.id, "experiment": frozen.id,
               "development_result": diagnostic.id, "development_observation": new_observation.id,
               "interpretation": interpretation.id, "decision": decision.id, "snapshot": snapshot.id}
        write_json(output / "ids.json", ids)
        if any(event["event_type"] == "object_reviewed" for r in records for event in brain.get_history(r["id"])):
            raise ValueError("Unexpected human review event in generated research thread")
        if inventory(source) != source_before or inventory(pilot_dir) != pilot_before:
            raise ValueError("Source corpus or archived pilot changed during run")
        report = ["# Brain-driven interpretability example", "", "Real local measurements; agent hypotheses and interpretations remain unreviewed.", "",
            "## What memory changed", "", f"The archived pilot's {accuracy:.0%} baseline made behavioral steering claims premature. The example froze a separate format diagnostic instead of tuning directions on those old results.", "",
            "## New development measurements", "", "| Format | Correct | Mean label probability mass | Frozen gate |", "|---|---:|---:|---|",
            *[f"| {c['template']} | {c['correct']}/{c['n']} | {c['mean_target_probability_mass']:.3f} | {c['gate_passed']} |" for c in result["conditions"]],
            "", "## Next action from the retrieved Brain packet", "", next_step["reason"], "",
            f"Basis: `{diagnostic.id}`. Thread: `{thread.id}`.", "", "## Boundaries", "",
            "This is a fixed, inspectable example policy, not an autonomous scientist or an LLM comparison. No human review events were synthesized. Measured records use EXPERIMENT_OBSERVED; ACCEPTED here marks imported measurements, not a scientific endorsement. All generated interpretations, hypotheses and decisions remain UNREVIEWED.", "",
            "Six handwritten development pairs do not establish generalization. Few-shot format changes several factors together; this does not isolate why the baseline changed. No new activation interventions or fresh steering holdout have been run.", "",
            "Literature hits and full block evidence are in literature.json. Before/after packets, IDs, auditable history, protocol, and all measurements are adjacent. Source corpus and previous pilot were unchanged."]
        with (output / "report.md").open("x") as stream:
            stream.write("\n".join(report) + "\n")
        write_json(output / "research-status.json", {"status": "complete", **protocol["budget"],
                   "source_unchanged": True, "pilot_unchanged": True, "human_reviews_created": 0,
                   "thread_id": thread.id, "next_action": next_step["action"]})
    except Exception as exc:
        write_json(output / "research-status.json", {"status": "failed", "error": str(exc)})
        raise
    finally:
        write_json(output / "research-checksums.json", inventory(output))
    return verify_research(output)
