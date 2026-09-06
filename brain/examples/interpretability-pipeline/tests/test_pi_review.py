from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from interp_pipeline.cli import write_json
from interp_pipeline.pi_review import review
from interp_pipeline.research import inventory


@pytest.mark.parametrize("outcome", ["success", "timeout", "failure"])
def test_review_explicit_spend_separate_output_and_preserved_env(tmp_path, monkeypatch, outcome):
    run = tmp_path / "run"
    run.mkdir()
    write_json(run / "research-status.json", {"status": "complete"})
    write_json(run / "ids.json", {"thread": "obj_0123456789abcdef"})
    write_json(run / "research-checksums.json", inventory(run))
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/run_pi_demo.py").write_text("# dummy runner")
    (repo / "integrations/pi").mkdir(parents=True)
    (repo / "integrations/pi/research-brain.ts").write_text("// dummy extension")
    python = tmp_path / "venv-python"
    python.symlink_to(sys.executable)
    output = tmp_path / "review"
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        assert command[0] == str(python)
        assert kwargs["env"]["RESEARCH_BRAIN_PYTHON"] == str(python)
        assert kwargs["env"]["RESEARCH_BRAIN_ROOT"] == str(run / "brain")
        assert kwargs.get("shell", False) is False
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 200)
        return SimpleNamespace(returncode=0 if outcome == "success" else 1, stdout="dummy", stderr="")
    monkeypatch.setattr(subprocess, "run", execute)
    assert review(run, repo, python, output)["provider_calls"] == 0
    assert not calls and not output.exists()
    result = review(run, repo, python, output, live=True)
    assert len(calls) == 1
    assert result["scientific_acceptance"] == "not_evaluated"
    assert result["returncode"] == {"success": 0, "timeout": 124, "failure": 1}[outcome]
    assert (output / "result.json").exists()
    with pytest.raises(FileExistsError):
        review(run, repo, python, output)
