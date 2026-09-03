"""Run the read-only Pi demo and preserve a machine-readable transcript."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


QUESTION = "What methods have I encountered that could help construct principled perturbation directions without gradients?"


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    transcript_dir = project / "artifacts" / "pi-demo"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    command = [
        "pi", "--print", "--mode", "json", "--no-builtin-tools", "--no-extensions",
        "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-session",
        "--extension", str(project / "integrations/pi/research-brain.ts"),
        "--append-system-prompt", str(project / "integrations/pi/RESEARCH_ASSISTANT.md"),
        "--model", "openai-codex/gpt-5.6-luna", QUESTION,
    ]
    environment = os.environ.copy()
    environment.setdefault("RESEARCH_BRAIN_ROOT", str(project / "data"))
    database = Path(environment["RESEARCH_BRAIN_ROOT"]) / "brain.sqlite3"
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    completed = subprocess.run(command, cwd=project, env=environment, text=True,
                               capture_output=True, timeout=180, check=False)
    after_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tool_calls = [
        {"tool": event.get("toolName"), "arguments": event.get("args")}
        for event in events if event.get("type") == "tool_execution_start"
    ]
    retrieved_ids: set[str] = set()
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
            records = payload if isinstance(payload, list) else [payload]
            retrieved_ids.update(
                record["record_id"] for record in records
                if isinstance(record, dict) and isinstance(record.get("record_id"), str)
            )
    final_answer = ""
    for event in events:
        message = event.get("message", {})
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            texts = [item.get("text", "") for item in message.get("content", []) if item.get("type") == "text"]
            if texts:
                final_answer = "\n".join(texts)
    artifact = {
        "timestamp": timestamp, "question": QUESTION, "model": "openai-codex/gpt-5.6-luna",
        "command": command, "returncode": completed.returncode, "built_in_tools_disabled": True,
        "brain_sha256_before": before_hash, "brain_sha256_after": after_hash,
        "brain_unchanged": before_hash == after_hash,
        "allowed_tools": ["research_recall", "research_evidence", "research_object"],
        "tool_calls": tool_calls, "retrieved_ids": sorted(retrieved_ids), "final_answer": final_answer,
        "stdout": completed.stdout, "stderr": completed.stderr,
    }
    destination = transcript_dir / f"{timestamp}.json"
    destination.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
