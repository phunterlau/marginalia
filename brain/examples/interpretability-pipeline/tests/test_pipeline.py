import json
from pathlib import Path

import numpy as np
import pytest
import torch

from interp_pipeline.cli import run, seal, verify, write_json
from interp_pipeline.core import budget, directions, summarize, validate
from interp_pipeline.model import QwenRunner, patch_output


@pytest.fixture
def spec():
    return json.loads((Path(__file__).parents[1] / "configs/sentiment.json").read_text())


def test_plan(spec):
    assert validate(spec) is spec
    assert sum(v for k, v in budget(spec).items() if k.endswith("forwards")) == 188


@pytest.mark.parametrize("change", [
    lambda s: s.update(revision="main"),
    lambda s: s.update(layer=-1),
    lambda s: s.update(seed=True),
    lambda s: s.update(alphas=[0, .1]),
    lambda s: s.update(alphas=[-.1, 0, .1, float("nan")]),
    lambda s: s["evaluation"][0].update(id=s["calibration"][0]["id"]),
    lambda s: s["evaluation"][0].update(positive=s["calibration"][0]["positive"].upper()),
    lambda s: s.update(positive_target=s["negative_target"]),
    lambda s: s.update(extra="unknown"),
])
def test_invalid_design(spec, change):
    change(spec)
    with pytest.raises(ValueError):
        validate(spec)


def test_directions_normalized_and_reproducible():
    p = np.array([[4., 1., 0.], [4., -1., 0.], [5., 0., 0.]])
    n = -p
    a, metadata = directions(p, n, 5, 19)
    b, _ = directions(p, n, 5, 19)
    for key in a:
        np.testing.assert_allclose(a[key], b[key])
        assert np.linalg.norm(a[key]) == pytest.approx(1)
    assert a["mean_difference"] @ a["pooled_pca"] > 0
    assert metadata["pca_explained_variance_fraction"] > .9
    with pytest.raises(ValueError):
        directions(p, p, 5, 19)


@pytest.mark.parametrize("as_tuple", [False, True])
def test_patch_only_last_token_and_preserves_original(as_tuple):
    source = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
    original = source.clone()
    out = patch_output((source, "cache") if as_tuple else source, torch.ones(4))
    if as_tuple:
        assert out[1] == "cache"
        out = out[0]
    assert torch.equal(source, original)
    assert torch.equal(out[:, :-1], source[:, :-1])
    assert torch.equal(out[:, -1], source[:, -1] + 1)


def test_real_tiny_qwen_hook_no_gradients():
    from transformers import Qwen3Config, Qwen3ForCausalLM
    model = Qwen3ForCausalLM(Qwen3Config(vocab_size=32, hidden_size=16,
        intermediate_size=32, num_hidden_layers=2, num_attention_heads=2,
        num_key_value_heads=2, head_dim=8)).eval().requires_grad_(False)
    ids = torch.tensor([[1, 2, 3]])
    with torch.inference_mode():
        baseline = model(ids).logits.clone()
        handle = model.model.layers[0].register_forward_hook(
            lambda m, a, o: patch_output(o, torch.zeros(16)))
        try:
            assert torch.equal(model(ids).logits, baseline)
        finally:
            handle.remove()
        handle = model.model.layers[0].register_forward_hook(
            lambda m, a, o: patch_output(o, torch.arange(16, dtype=torch.float32)))
        try:
            changed = model(ids).logits
            assert torch.equal(changed[:, :-1], baseline[:, :-1])
            assert not torch.allclose(changed[:, -1], baseline[:, -1])
        finally:
            handle.remove()
    assert all(p.grad is None for p in model.parameters())


def test_paired_summary():
    rows = [{"method": method, "alpha": .1, "pair_id": group, "gap_delta": delta,
             "kl_from_baseline": .01, "correct": True}
            for group in ("a", "b") for method, delta in (("mean_difference", 2), ("random_000", .5))
            for _ in range(2)]
    result = summarize(rows, 1)[0]
    assert result["n_pairs"] == 2
    assert result["excess_over_random_mean"]["mean"] == 1.5
    assert result["gap_delta"]["pair_bootstrap_95_percentile"] == [2, 2]


def test_forward_failure_removes_hook(spec):
    from transformers import BatchEncoding
    class Tokenizer:
        def __call__(self, prompt, **kwargs):
            return BatchEncoding({"input_ids": torch.tensor([[1, 2]])})
        def encode(self, text, **kwargs):
            return [1, 2, 3 if text.endswith(" positive") else 4]
    class FailingModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.layer = torch.nn.Identity()
        def forward(self, **kwargs):
            self.layer(torch.ones(1, 2, 4))
            raise ValueError("injected forward failure")
    runner = QwenRunner.__new__(QwenRunner)
    runner.model = FailingModel()
    runner.layer = runner.model.layer
    runner.tokenizer = Tokenizer()
    runner.spec, runner.calls, runner.targets = spec, 0, [3, 4]
    with pytest.raises(ValueError, match="injected forward"):
        runner.forward("Test")
    assert len(runner.layer._forward_hooks) == 0
    assert runner.calls == 0


def test_plan_is_provider_and_model_free(spec, tmp_path, monkeypatch, capsys):
    from interp_pipeline.cli import main
    config = tmp_path / "config.json"
    config.write_text(json.dumps(spec))
    monkeypatch.setattr("sys.argv", ["interpret", "plan", str(config)])
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected model or network access")
    monkeypatch.setattr("interp_pipeline.model.QwenRunner", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    main()
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert list(tmp_path.iterdir()) == [config]


def test_checksums_and_exclusive_writes(tmp_path):
    write_json(tmp_path / "status.json", {"status": "complete"})
    seal(tmp_path)
    assert verify(tmp_path)["integrity"] == "verified"
    with pytest.raises(FileExistsError):
        write_json(tmp_path / "status.json", {})
    (tmp_path / "status.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        verify(tmp_path)


def test_failed_run_is_sealed_and_cannot_overwrite(spec, tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("injected load failure")
    monkeypatch.setattr("interp_pipeline.model.QwenRunner", fail)
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="injected"):
        run(spec, output, None)
    assert verify(output)["status"] == "failed"
    with pytest.raises(FileExistsError):
        run(spec, output, None)


def test_mocked_end_to_end(spec, tmp_path, monkeypatch):
    class FakeRunner:
        def __init__(self, spec, cache):
            self.calls, self.targets = 0, [0, 1]
        def forward(self, text, vector=None):
            self.calls += 1
            activation = np.array([len(text), sum(map(ord, text)) % 30, 1.], dtype=float)
            effect = 0.0 if vector is None else float(vector[0])
            return activation, torch.tensor([effect, -effect]).log_softmax(-1), [1, 2]
    monkeypatch.setattr("interp_pipeline.model.QwenRunner", FakeRunner)
    output = tmp_path / "mocked"
    assert run(spec, output, None)["status"] == "complete"
    observed = json.loads((output / "observations.json").read_text())
    assert observed["forward_passes"] == 188
    assert observed["provider_calls"] == observed["backward_passes"] == 0
    assert len(observed["conditions"]) == 21
    assert json.loads((output / "interpretations.json").read_text())["claims"] == []
    rows = json.loads((output / "measurements.json").read_text())["interventions"]
    for alpha in spec["alphas"]:
        norms = [r["injection_l2"] for r in rows if r["alpha"] == alpha]
        np.testing.assert_allclose(norms, norms[0], atol=1e-5)
