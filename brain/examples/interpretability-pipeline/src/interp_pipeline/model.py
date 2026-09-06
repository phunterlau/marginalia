"""Pinned offline Qwen adapter. Hooks touch one decoder output at one token."""
import torch
from huggingface_hub import snapshot_download
from transformers import AutoModelForCausalLM, AutoTokenizer


def patch_output(output, vector):
    hidden = output[0] if isinstance(output, tuple) else output
    changed = hidden.clone()
    changed[:, -1, :] += vector.to(device=hidden.device, dtype=hidden.dtype)
    return (changed,) + output[1:] if isinstance(output, tuple) else changed


class QwenRunner:
    def __init__(self, spec, cache_dir=None):
        torch.set_num_threads(4)
        torch.manual_seed(spec["seed"])
        torch.use_deterministic_algorithms(True)
        options = dict(revision=spec["revision"], local_files_only=True,
                       trust_remote_code=False, cache_dir=cache_dir)
        self.snapshot = snapshot_download(spec["model_id"], revision=spec["revision"],
                                          cache_dir=cache_dir, local_files_only=True)
        self.tokenizer = AutoTokenizer.from_pretrained(spec["model_id"], **options)
        self.model = AutoModelForCausalLM.from_pretrained(
            spec["model_id"], dtype=torch.float32, attn_implementation="eager", **options)
        if self.model.config._commit_hash != spec["revision"]:
            raise ValueError("Loaded checkpoint revision does not match pinned revision")
        self.model.to(spec["device"]).eval().requires_grad_(False)
        self.layer = self.model.model.layers[spec["layer"]]
        self.spec = spec
        self.calls = 0
        self.targets = []
        for key in ("positive_target", "negative_target"):
            tokens = self.tokenizer.encode(spec[key], add_special_tokens=False)
            if len(tokens) != 1:
                raise ValueError(f"{key} must encode to exactly one token, got {tokens}")
            self.targets.append(tokens[0])
        if self.targets[0] == self.targets[1]:
            raise ValueError("Targets resolve to the same token")

    def forward(self, text, vector=None):
        prompt = self.spec["template"].format(text=text)
        inputs = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        if inputs.input_ids.shape[1] > self.spec["max_tokens"]:
            raise ValueError("Prompt exceeds token limit; truncation is forbidden")
        for target, target_id in zip(("positive_target", "negative_target"), self.targets):
            combined = self.tokenizer.encode(prompt + self.spec[target], add_special_tokens=False)
            if combined != inputs.input_ids[0].tolist() + [target_id]:
                raise ValueError("Target tokenization is not stable at prompt boundary")
        captured = []

        def hook(module, args, output):
            hidden = output[0] if isinstance(output, tuple) else output
            captured.append(hidden[0, -1].detach().float().cpu().numpy().copy())
            return output if vector is None else patch_output(output, vector)

        handle = self.layer.register_forward_hook(hook)
        try:
            with torch.inference_mode():
                out = self.model(**{k: v.to(self.spec["device"]) for k, v in inputs.items()}, use_cache=False)
                logprobs = out.logits[0, -1].float().log_softmax(-1).cpu()
            self.calls += 1
        finally:
            handle.remove()
        if len(captured) != 1 or not torch.isfinite(logprobs).all():
            raise ValueError("Invalid hook capture or nonfinite logits")
        return captured[0], logprobs, inputs.input_ids[0].tolist()
