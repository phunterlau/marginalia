"""Run the read-only Pi demo and preserve a machine-readable transcript."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


QUESTION = "What methods have I encountered that could help construct principled perturbation directions without gradients?"


ALLOWED_TOOLS = {"research_context", "research_recall", "research_evidence", "research_object"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and verify the read-only Pi Research Brain demo")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--model", default="openai-codex/gpt-5.6-luna")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.timeout <= 600:
        raise ValueError("timeout must be between 1 and 600 seconds")
    project = Path(__file__).resolve().parents[1]
    transcript_dir = Path(
        args.output_dir
        or os.getenv("PI_DEMO_TRANSCRIPT_DIR", "")
        or project / "artifacts" / "pi-demo"
    ).expanduser().resolve()
    transcript_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    pi_binary = os.getenv("PI_BINARY") or shutil.which("pi")
    if not pi_binary:
        raise RuntimeError("Pi executable was not found; install Pi or set PI_BINARY")
    command = [
        pi_binary, "--print", "--mode", "json", "--no-builtin-tools", "--no-extensions",
        "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-session",
        "--extension", str(project / "integrations/pi/research-brain.ts"),
        "--append-system-prompt", str(project / "integrations/pi/RESEARCH_ASSISTANT.md"),
        "--model", args.model, args.question,
    ]
    environment = os.environ.copy()
    environment.setdefault("RESEARCH_BRAIN_ROOT", str(project / "data"))
    database = Path(environment["RESEARCH_BRAIN_ROOT"]) / "brain.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Research Brain database not found: {database}")
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    failure: dict[str, str] | None = None
    try:
        completed = subprocess.run(
            command, cwd=project, env=environment, text=True,
            capture_output=True, timeout=args.timeout, check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        failure = {"type": "TimeoutExpired", "message": f"Pi exceeded {args.timeout} seconds"}
    except OSError as exc:
        returncode = 126
        stdout, stderr = "", str(exc)
        failure = {"type": type(exc).__name__, "message": str(exc)}
    after_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tool_calls = [
        {"tool": event.get("toolName"), "arguments": event.get("args")}
        for event in events if event.get("type") == "tool_execution_start"
    ]
    retrieved_ids: set[str] = set()

    def collect_ids(value: object) -> None:
        if isinstance(value, dict):
            for key in ("record_id", "block_id", "object_id", "id"):
                if isinstance(value.get(key), str) and value[key].startswith(("block_", "obj_")):
                    retrieved_ids.add(value[key])
            for item in value.values():
                collect_ids(item)
        elif isinstance(value, list):
            for item in value:
                collect_ids(item)

    for event in events:
        if event.get("type") != "tool_execution_end":
            continue
        for content in event.get("result", {}).get("content", []):
            if content.get("type") != "text":
                continue
            try:
                payload = json.loads(content["text"])
            except (json.JSONDecodeError, TypeError):
                continue
            collect_ids(payload)
    final_answer = ""
    for event in events:
        message = event.get("message", {})
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            texts = [item.get("text", "") for item in message.get("content", []) if item.get("type") == "text"]
            if texts:
                final_answer = "\n".join(texts)
    failures = []
    if returncode != 0:
        failures.append(f"Pi exited with status {returncode}")
    if before_hash != after_hash:
        failures.append("Research Brain database changed during the read-only demo")
    if not tool_calls:
        failures.append("Pi made no Research Brain tool calls")
    unexpected_tools = sorted({item["tool"] for item in tool_calls if item["tool"] not in ALLOWED_TOOLS})
    if unexpected_tools:
        failures.append(f"Pi invoked tools outside the read-only allowlist: {unexpected_tools}")
    if not final_answer.strip():
        failures.append("Pi produced no final answer")
    cited_block_ids = sorted(set(re.findall(r"\bblock_[a-f0-9]{12,64}\b", final_answer)))
    ungrounded_block_ids = sorted(set(cited_block_ids) - retrieved_ids)
    if len(cited_block_ids) < 2:
        failures.append("Pi answer cited fewer than two exact evidence blocks")
    if ungrounded_block_ids:
        failures.append(f"Pi answer cited blocks absent from tool output: {ungrounded_block_ids}")
    if not re.search(r"\bv[1-9][0-9]*\b", final_answer):
        failures.append("Pi answer cited no explicit paper revision")
    if not re.search(r"\b[^\s`]+\.(?:tex|md|pdf)(?::|`,|`|\s)", final_answer, re.IGNORECASE):
        failures.append("Pi answer cited no source member")

    artifact = {
        "timestamp": timestamp, "question": args.question, "model": args.model,
        "command": command, "returncode": returncode, "built_in_tools_disabled": True,
        "brain_sha256_before": before_hash, "brain_sha256_after": after_hash,
        "brain_unchanged": before_hash == after_hash,
        "allowed_tools": sorted(ALLOWED_TOOLS),
        "tool_calls": tool_calls, "retrieved_ids": sorted(retrieved_ids), "final_answer": final_answer,
        "gate_passed": not failures,
        "gate_failures": failures,
        "citation_validation": {
            "cited_block_ids": cited_block_ids,
            "ungrounded_block_ids": ungrounded_block_ids,
        },
        "failure": failure,
        "stdout": stdout, "stderr": stderr,
    }
    destination = transcript_dir / f"{timestamp}.json"
    destination.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    return 0 if artifact["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
