"""Explicitly opt-in network synthesis through the existing read-only Pi client."""
import os
from pathlib import Path
import subprocess

from .cli import file_hash, write_json
from .research import disjoint, load, verify_research


def review(run_dir, brain_repo, brain_python, output, *, live=False):
    # Do not resolve the venv interpreter symlink: its invoked path selects the env.
    brain_python = Path(brain_python).expanduser().absolute()
    run_dir, brain_repo, output = [Path(p).resolve() for p in (run_dir, brain_repo, output)]
    if verify_research(run_dir)["status"] != "complete":
        raise ValueError("Pi requires a complete, verified research run")
    disjoint(output, [run_dir, brain_repo])
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    runner = brain_repo / "scripts/run_pi_demo.py"
    if not runner.is_file() or not brain_python.is_file():
        raise ValueError("Supply the existing Brain repo and Python interpreter")
    thread_id = load(run_dir / "ids.json")["thread"]
    question = (
        f"Use research_context with thread_id {thread_id}, mode analysis, limit 8, "
        "and query sentiment baseline development diagnostic result. Review this real forward-only Qwen interpretability thread. "
        "Explain the archived intervention pilot and new baseline diagnostic, including sample units. "
        "Distinguish EXPERIMENT_OBSERVED records from UNREVIEWED proposals; accepted measurement records are not human scientific endorsement. "
        "Retrieve two relevant activation steering/contrastive direction passages with research_recall and research_evidence; "
        "cite exact block IDs, revisions and TeX source members. Propose one fresh-holdout experiment with frozen controls and "
        "an explicit primary metric. Audit your proposed metric: a fixed positive sentiment direction should not be expected "
        "to improve negative-example accuracy, and both signs should not be required to improve accuracy on a balanced set. "
        "Separate direction controllability from classification improvement and from general semantic interpretation. "
        "Do not introduce a chat-template change without declaring a new protocol. Do not claim a proposed experiment has run. "
        "Keep the answer below 800 words. Do not mutate Brain data.")
    command = [str(brain_python), str(runner), "--question", question,
               "--model", "openai-codex/gpt-5.6-luna", "--timeout", "180", "--output-dir", str(output)]
    if not live:
        return {"mode": "dry-run", "writes": 0, "provider_calls": 0, "command": command}
    output.mkdir(parents=True)
    environment = os.environ.copy()
    environment.update(RESEARCH_BRAIN_PYTHON=str(brain_python), RESEARCH_BRAIN_ROOT=str(run_dir / "brain"),
                       PYTHONPATH=str(brain_repo / "src"))
    write_json(output / "request.json", {"command": command, "runner_sha256": file_hash(runner),
        "extension_sha256": file_hash(brain_repo / "integrations/pi/research-brain.ts"),
        "review_state": "UNREVIEWED", "scope": "citation/tool integrity is not scientific acceptance"})
    try:
        completed = subprocess.run(command, env=environment, capture_output=True, text=True,
                                   timeout=200, check=False)
        result = {"returncode": completed.returncode, "stdout": completed.stdout,
                  "stderr": completed.stderr, "scientific_acceptance": "not_evaluated"}
    except subprocess.TimeoutExpired:
        result = {"returncode": 124, "error": "Pi wrapper timed out", "scientific_acceptance": "not_evaluated"}
    except OSError as exc:
        result = {"returncode": 126, "error": str(exc), "scientific_acceptance": "not_evaluated"}
    write_json(output / "result.json", result)
    verify_research(run_dir)
    return result
