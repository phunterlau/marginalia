from __future__ import annotations

from pathlib import Path
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import shutil
import pytest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
EXTENSION = (PROJECT / "integrations" / "pi" / "research-brain.ts").read_text(encoding="utf-8")
INSTRUCTIONS = (PROJECT / "integrations" / "pi" / "RESEARCH_ASSISTANT.md").read_text(encoding="utf-8")


def test_native_pi_loads_extension_without_provider_calls(tmp_path):
    binary = shutil.which("pi")
    if binary is None:
        pytest.skip("Pi is optional; native extension load requires local Pi")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "ANTHROPIC_"))}
    env["PI_CODING_AGENT_DIR"] = str(tmp_path / "isolated-agent")
    result = subprocess.run([binary, "--mode", "rpc", "--no-session", "--no-tools", "--no-extensions",
                             "--no-skills", "--no-prompt-templates", "--no-context-files", "-e",
                             str(PROJECT / "integrations/pi/research-brain.ts")],
                            input='{"type":"get_state","id":"load-check"}\n',
                            text=True, capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert any(r.get("id") == "load-check" and r.get("success") for r in replies)


def test_pi_recall_exposes_all_bounded_retrieval_filters() -> None:
    for field in (
        "gradients_required",
        "training_required",
        "activation_access",
        "weight_access",
        "representation_kind",
        "origins",
        "review_states",
        "document_id",
        "version_label",
    ):
        assert f"{field}: Type.Optional" in EXTENSION
    assert 'args.push("--filters", JSON.stringify(params.filters))' in EXTENSION
    assert "additionalProperties: false" in EXTENSION
    assert 'Type.Literal("SOURCE_EXPLICIT")' in EXTENSION
    assert 'Type.Literal("INVALIDATED")' in EXTENSION


def test_pi_boundary_uses_fixed_execfile_and_bounded_valid_json() -> None:
    assert "execFile(" in EXTENSION
    assert "exec(" not in EXTENSION
    assert "MAX_RECALL_LIMIT = 8" in EXTENSION
    assert "MAX_EVIDENCE_PER_HIT = 4" in EXTENSION
    assert "MAX_TOOL_OUTPUT_CHARS = 64_000" in EXTENSION
    assert "JSON.stringify(compact.slice(0, returned), null, 2)" in EXTENSION
    assert "stdout.slice(" not in EXTENSION


def test_pi_instructions_require_filters_and_corpus_mismatch() -> None:
    assert "encode them in the tool's structured `filters`" in INSTRUCTIONS
    assert "A false access constraint excludes cards where the field" in INSTRUCTIONS
    assert "the corpus does not establish it" in INSTRUCTIONS
    assert "keep `limit` at most 5" in INSTRUCTIONS


def test_pi_exposes_bounded_frontier_context_without_mutation_tools() -> None:
    assert 'name: "research_context"' in EXTENSION
    assert 'args = ["context", params.question' in EXTENSION
    assert 'Type.Literal("critique")' in EXTENSION
    assert 'Type.Literal("brainstorm")' in EXTENSION
    assert 'blind_first: Type.String' in EXTENSION
    assert 'if ("blind_first" in params)' in EXTENSION
    assert "Never send `blind_first` in another mode" in INSTRUCTIONS
    assert "Research packet exceeds the tool output limit" in EXTENSION
    for forbidden in ("research_ingest", "research_extract", "research_review", "research_delete"):
        assert forbidden not in EXTENSION


def _load_demo_module() -> object:
    path = PROJECT / "scripts" / "run_pi_demo.py"
    spec = importlib.util.spec_from_file_location("run_pi_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pi_demo_runner_always_emits_a_machine_readable_gate_artifact() -> None:
    module = _load_demo_module()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        brain_root = root / "brain"
        brain_root.mkdir()
        database = brain_root / "brain.sqlite3"
        database.write_bytes(b"immutable-fixture")
        result_payload = json.dumps([{
            "record_id": "obj_abc123def456",
            "evidence": [
                {"block_id": "block_abc123def456", "source_member": "paper.tex"},
                {"block_id": "block_789abc123def", "source_member": "paper.tex"},
            ],
        }])
        stdout = "\n".join([
            json.dumps({
                "type": "tool_execution_start", "toolName": "research_recall",
                "args": {"query": "test"},
            }),
            json.dumps({
                "type": "tool_execution_end",
                "result": {"content": [{"type": "text", "text": result_payload}]},
            }),
            json.dumps({
                "type": "message_end",
                "message": {"role": "assistant", "content": [{
                    "type": "text",
                    "text": "Answer from v3 paper.tex:12: block_abc123def456 and block_789abc123def.",
                }]},
            }),
        ])
        completed = subprocess.CompletedProcess(["pi"], 0, stdout=stdout, stderr="")
        environment = {
            "RESEARCH_BRAIN_ROOT": str(brain_root),
            "PI_BINARY": "/test/pi",
        }
        with patch.dict(os.environ, environment, clear=False), patch.object(
            module.subprocess, "run", return_value=completed
        ) as invoked:
            assert module.main(["--output-dir", str(root / "artifacts")]) == 0
        artifact_path = next((root / "artifacts").glob("*.json"))
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["gate_passed"]
        assert artifact["brain_unchanged"]
        assert artifact["retrieved_ids"] == [
            "block_789abc123def", "block_abc123def456", "obj_abc123def456",
        ]
        assert artifact["citation_validation"]["ungrounded_block_ids"] == []
        assert artifact["tool_calls"][0]["tool"] == "research_recall"
        assert hashlib.sha256(database.read_bytes()).hexdigest() == artifact["brain_sha256_after"]
        command = invoked.call_args.args[0]
        assert "--no-builtin-tools" in command
        assert "--no-session" in command


def test_pi_demo_runner_writes_failure_artifact_on_timeout() -> None:
    module = _load_demo_module()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        brain_root = root / "brain"
        brain_root.mkdir()
        (brain_root / "brain.sqlite3").write_bytes(b"immutable-fixture")
        environment = {"RESEARCH_BRAIN_ROOT": str(brain_root), "PI_BINARY": "/test/pi"}
        timeout = subprocess.TimeoutExpired(["pi"], 2, output="partial", stderr="timed out")
        with patch.dict(os.environ, environment, clear=False), patch.object(
            module.subprocess, "run", side_effect=timeout
        ):
            assert module.main(["--output-dir", str(root / "artifacts"), "--timeout", "2"]) == 1
        artifact = json.loads(next((root / "artifacts").glob("*.json")).read_text(encoding="utf-8"))
        assert not artifact["gate_passed"]
        assert artifact["returncode"] == 124
        assert artifact["failure"]["type"] == "TimeoutExpired"
