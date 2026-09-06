# Interpretability research with Research Brain

A standalone, forward-only Qwen experiment showing how research memory informs
the next experiment. The runner works independently; optional Brain and Pi adapters
add source evidence, research history, and read-only synthesis.

## Research question and walkthrough

**Can activation-derived sentiment directions steer an LLM more specifically than
matched-norm random directions, without gradients or training?**

1. **Run a pilot.** On pinned Qwen3-0.6B, construct mean-difference and pooled-PCA
   directions from six positive/negative topic pairs. Compare them with five seeded,
   matched-norm random directions on four different topic pairs. Patch decoder block
   13 (zero-based), final prompt token only, at strengths -0.1, 0, +0.1 times the
   calibration mean residual norm. No fitting on evaluation data.
2. **Remember the limitation.** The unmodified two-label baseline scored only 50%.
   Import measurements and an insufficient-evidence record into an isolated Brain
   thread. Do not promote activation effects to useful behavioral steering.
3. **Retrieve literature.** Local reliable searches retrieved ActAdd (*Steering
   Language Models With Activation Engineering*, v5), CAA (*Steering Llama 2 via
   Contrastive Activation Addition*, v4), Representation Engineering (v4), and
   Refusal Direction (v3). Exact TeX blocks retain revisions and source locators.
   Some initial hits were headings or comments, not useful supporting passages;
   Pi subsequently fetched substantive ActAdd/CAA passages. These are methodological
   references, not validation of this experiment or a claimed paper reproduction.
4. **Select a diagnostic from memory.** A programmed policy reads a Brain critique
   packet and selects baseline diagnosis when retrieved accuracy is below 80%.
   This is an explicit example rule, not an autonomously discovered strategy.
5. **Freeze and measure.** Persist the diagnostic protocol before 36 model forwards.
   Compare the original prompt with two few-shot demonstration orders on six new
   development topic pairs. No new activation intervention is performed here.
6. **Update the frontier.** Store observations separately from unreviewed explanations.
   Read the result through a new Brain packet; propose a fresh steering holdout if
   both formats qualify. Missing evidence cannot silently pass.
7. **Optional Pi handoff.** Pi reads the thread and exact paper passages, cites them,
   and proposes an experiment. Built-in tools are disabled; Pi cannot execute the
   experiment or mutate Brain data.

## Results and interpretation

These are real CPU float32 measurements, not mock outputs. Six handwritten
development topic pairs do not establish generalization.

| Development format | Correct / 12 | Mean probability assigned to the two labels | Gate |
|---|---:|---:|---|
| Original | 6 | 0.217 | Failed |
| Few-shot, positive demonstration first | 11 | 0.917 | Passed |
| Few-shot, negative demonstration first | 12 | 0.926 | Passed |

The frozen gate requires **both** few-shot orders to achieve at least 10/12 correct,
5/6 per label, and mean label probability mass at least 0.5. This qualifies a
development readout only. Several prompt properties changed together, so it does
not isolate why performance improved.

In the older intervention pilot, at +0.1, mean-difference changed the positive-minus-
negative logit gap by about +0.171, PCA by +0.196, and the average random direction
by +0.015. Mean/PCA cosine was about 0.996: they are not independent mechanisms.
Baseline accuracy was 4/8. Two local pilot runs reproduced measurements, using 188
forwards each; the later diagnostic used another 36. No gradients or provider calls
were involved in those experiments.

**Next proposed step:** freeze fresh topics/templates and evaluate each steering sign
and format against matched random controls. That fresh steering holdout has **not**
run. Do not reuse development data as fresh evidence.

Lightweight measured data included here:

- [Baseline measurements](results/baseline-measurements.json): 36 prompt/format observations, token IDs, gaps, top tokens.
- [Baseline summary](results/baseline-summary.json): counts and frozen-gate outcomes.
- [Pilot summary](results/pilot-summary.json): all direction/strength aggregates, bootstrap intervals, accuracy, and KL.

Full archives, Brain databases, activation arrays, original run bundles, Pi transcripts,
and session notes remain local. These summaries are not complete replay bundles or
authenticated scientific records. Paper redistribution requires a license check.

### Pi findings are not scientific acceptance

Two real Luna synthesis sessions passed read-only tool/citation checks and left the
research run unchanged. The first proposal nevertheless required both steering signs
to improve classification accuracy—an unsound criterion for a fixed sentiment
direction. The follow-up corrected that but understated the random-control count
and needed stronger candidate-versus-random, per-sign tests. Long condition lists
are compacted in the current Brain packet; consult complete results before inferring
the whole design. Both proposals remain unreviewed.

## Modules and boundaries

| Module | Role |
|---|---|
| `core.py` | Validate splits/configuration; normalize directions; paired analysis. |
| `model.py` | Load pinned cached Qwen; capture/patch one decoder output token. |
| `cli.py` | Explicit compute commands, new-only output directories, integrity checks. |
| `development.py` | Frozen prompt-format diagnostic and both-order gate. |
| `research.py` | Isolated Brain copy, literature, typed history, before/after packets, bounded policies. |
| `pi_review.py` | Opt-in synthesis through the existing read-only Pi runner. |
| Brain core | SQLite/source assets, reliable retrieval, provenance, audit history, ResearchPacket. |

This run used lexical retrieval, with no new embeddings or canonical extraction.
The reviewer UI is not involved. Agent-authored hypotheses, explanations and decisions
remain `UNREVIEWED`. Accepted `EXPERIMENT_OBSERVED` records denote imported measurements,
not fabricated human scientific approval. No review events or source-corpus writes occur.

## Setup

From the repository root, with `uv` installed:

```sh
cd brain
uv sync --extra dev
cd examples/interpretability-pipeline
uv sync --locked --extra test
export RESEARCH_EXAMPLE_BRAIN="$(cd ../.. && pwd)"
export PYTHONPATH="$RESEARCH_EXAMPLE_BRAIN/src"
.venv/bin/pytest -q
```

Python 3.12+ is required; the original run used Python 3.13 on Apple Silicon. The
lockfile pins experiment dependencies. Brain comes from this checkout via `PYTHONPATH`,
not a registry package. Without that path, three Brain integration tests skip.
Tests use synthetic models/data and no provider calls; an additional test verifies
the published measurements against the frozen protocol and summary.

Verification for this packaged example: **31 tests passed**, including all Brain
integration cases and the published-results consistency check.

## Run locally

Replace these placeholders with an existing compatible Brain corpus and a populated
Hugging Face cache:

```sh
export RESEARCH_EXAMPLE_DATA=/absolute/path/to/existing/brain-data
export RESEARCH_EXAMPLE_CACHE=/absolute/path/to/huggingface-cache
```

The cache must contain `Qwen/Qwen3-0.6B` revision
`c1899de289a04d12100db370d81485cdf75e47ca`. Missing weights fail visibly. Experiments
never download weights or execute remote model code; dependency installation may
use the network. CPU float32 is verified; MPS/CUDA are not. Prompts are plain
completions, not chat templates.

Produce a complete local pilot with provenance:

```sh
.venv/bin/interpret plan configs/sentiment.json
.venv/bin/interpret run configs/sentiment.json \
  --cache-dir "$RESEARCH_EXAMPLE_CACHE" --output artifacts/pilot
.venv/bin/interpret verify artifacts/pilot
```

Plan, then explicitly authorize the Brain diagnostic:

```sh
# No writes or model execution by default.
.venv/bin/interpret research \
  --source-brain "$RESEARCH_EXAMPLE_DATA" --pilot artifacts/pilot \
  --protocol configs/baseline-development.json --output artifacts/research \
  --cache-dir "$RESEARCH_EXAMPLE_CACHE"

# --live here authorizes 36 LOCAL forwards, not an API call.
.venv/bin/interpret research \
  --source-brain "$RESEARCH_EXAMPLE_DATA" --pilot artifacts/pilot \
  --protocol configs/baseline-development.json --output artifacts/research \
  --cache-dir "$RESEARCH_EXAMPLE_CACHE" --live
.venv/bin/interpret research-verify artifacts/research
```

Outputs include a report, isolated `brain/`, original `pilot/`, new `development/`,
literature and exact evidence, before/after packets, policies, thread history and IDs.
Loaded Brain Python sources, experiment implementation, model hashes, and runtime
versions are preserved. Output directories must be new; failed runs are retained.
Integrity checks detect changed files, not scientific invalidity or maliciously
rewritten hashes. Copy the sealed Brain before further research or human reviews.

Optional Pi synthesis uses its existing login and `openai-codex/gpt-5.6-luna`:

```sh
.venv/bin/interpret pi-review artifacts/research \
  --brain-repo "$RESEARCH_EXAMPLE_BRAIN" \
  --brain-python "$RESEARCH_EXAMPLE_BRAIN/.venv/bin/python" \
  --output artifacts/pi-review
# Add --live to authorize network synthesis. Default is a no-call dry-run.
```

Pi outputs proposals, not instructions to execute automatically. Review sample units,
controls, omitted context and success criteria first.

Implementation references: [Qwen3](https://huggingface.co/docs/transformers/model_doc/qwen3),
[PyTorch hooks](https://docs.pytorch.org/docs/stable/generated/torch.nn.Module.html#torch.nn.Module.register_forward_hook).
