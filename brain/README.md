# Research Brain

## Approved paper absorption

The [scoped absorption CLI](docs/absorption.md) turns an arXiv URL into source
evidence and an explicit paid-work approval, with durable task checkpoints and
call/token admission budgets. Backend acceptance remains in progress; see the
guide's validation boundaries before unattended use.

## Worked research example

[Forward-only interpretability pipeline](examples/interpretability-pipeline/README.md)
connects a real Qwen activation-steering pilot to Brain evidence retrieval, an
isolated research thread, a frozen baseline diagnostic, and a read-only Pi handoff.
It includes runnable code and lightweight measured results, with explicit boundaries
between observations, unreviewed interpretations, and proposed next experiments.

## Local memory reviewer

The optional reviewer presents Method/Math cards beside their exact source evidence.
It runs locally, makes no model calls, and exposes only read operations plus audited
accept/dispute/reject decisions. Acceptance means source-faithful extraction and
makes a card eligible for reliable recall; it does not certify scientific truth.

From this `brain` directory:

```bash
uv sync --extra reviewer --extra dev
cd reviewer-ui
npm ci
npm run build
cd ..
uv run research --root /absolute/path/to/existing/data review-ui --port 8765
```

Open `http://127.0.0.1:8765`. An existing compatible database is required; this command
does not initialize or migrate it. The server binds only to loopback. Disputes and
rejections require notes; stale submissions must be reloaded before retrying.
Skip does not write a review. Notes survive card navigation within the page.

Frontend build output is ignored by Git and bundled into Python packages when built
before packaging. No Node runtime is required to serve a built reviewer. Full-source
data and generated review artifacts must remain local.

Verification (browser tests mock all API responses and never accept real cards):

```bash
uv run pytest -q
# With the built reviewer running and Google Chrome installed:
cd reviewer-ui
npm test
cd ..
uv run python scripts/verify_reviewer_corpus.py --root /absolute/path/to/isolated/corpus-copy
```

The real-corpus check verifies evidence fidelity and unchanged database bytes. Human
review is a separate step; neither test suite marks real cards accepted. Canvas,
card editing, bulk review, and remote access are intentionally absent.

Research Brain is an evidence-first, headless research memory for Pi and other
agent clients. Original sources and exact locators remain canonical; extraction,
embeddings, and generated connections are derived and explicitly labeled.

## ResearchPacket acceptance checks

Run the eight frontier contracts offline on an isolated synthetic research history:

```bash
research evaluate-frontier evals/frontier-v1.json --fixture
research evaluate-frontier evals/frontier-v1.json --fixture --output /tmp/frontier-report.json
```

`--fixture` never creates or changes the selected `--root`. To check an existing
research thread instead, use `research --root /path/to/data evaluate-frontier
evals/frontier-v1.json --thread obj_ID`. This requires a compatible database and
makes no provider calls. Report files are created exclusively, never overwritten.
A failed contract exits with status 1; invalid inputs exit with status 2.

Reports include complete packets, failed assertions, source/spec digests, elapsed
time, and equal-count lexical raw-top-k IDs. This is structural coverage, **not** a
judged answer-quality or brainstorming-usefulness score. Synthetic observations
and acceptance states are test fixtures, not actual research findings.

Frontier packets rank thread records by deterministic lexical overlap rather than
recency alone. Observation packets can include explicitly referenced experiment
results as separate records; critique packets include hypotheses alongside
counterevidence. Question items expose `question_links` (up to eight stored,
directional, epistemically labeled links) and `question_links_omitted`. These are
read-only packet projections; no graph edges or review states are changed.

## Judged research comparisons

Preview two matched comparisons with no API calls or output artifacts:

```bash
research compare-research evals/comparison-v1.json --fixture --dry-run
```

Explicitly enable live generation/judging and choose a fresh output directory:

```bash
research compare-research evals/comparison-v1.json --fixture --live \
  --output-dir /absolute/path/to/local/comparison-run
```

Replace `--fixture` with `--thread obj_ID` and select an existing `--root` for a
real research thread. The source database is snapshot-copied; answers and generation
attempts go into the new artifact directory, never into reliable memory. No cards
are accepted, changed, or generated there. Full snapshots and responses stay local.

Each case uses nine logical calls: a blind draft, revisions with and without memory,
raw-top-k and ResearchPacket answers, and two anonymous order-swapped judgments for
each comparison. Retrieval is lexical-only for both paths. Generation uses equal
output limits and the same model; input ceilings match but actual lengths differ.
The judge receives each answer's evidence availability so it does not punish a
blind answer for correctly stating that no results were supplied.

Defaults: `RESEARCH_COMPARE_MODEL=gpt-5.6-luna`, `RESEARCH_JUDGE_MODEL` defaults to the
generator, and `RESEARCH_REASONING_EFFORT=medium`. Credentials are environment-only
`OPENAI_API_KEY`; the optional OpenAI dependency must be installed. Responses use
strict JSON schemas, `store=false`, a 60-second timeout, and a 4,096-output-token
ceiling. Hidden SDK retries are disabled; only network/timeouts, 429, and 5xx get
up to two explicit retries. Refused, incomplete, and invalid outputs fail the run
while retaining response IDs, raw output, and available usage.

Artifacts include a separate generation ledger, exact requests/responses, answers,
anonymous human-review JSON, score breakdowns, order-sensitivity flags, and a final
report. Existing output directories are never overwritten. A successful exit means
the harness completed, **not** that memory improved research: human review remains
pending, history-awareness gains are separated from other dimensions, and one
synthetic trial with a same-model judge cannot establish general usefulness.

The optional adapter follows the official [Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## What works

- immutable source assets and source revisions;
- separately versioned parser compilations;
- source-first arXiv ingestion with pinned offline fixtures and PDF fallback;
- in-place TeX include traversal and exact member/line/character locators;
- evidence-linked Method and Math cards with review state;
- opt-in GPT-5.6 Luna strict structured extraction;
- opt-in `text-embedding-3-small` indexing and FTS/cosine RRF retrieval;
- exploratory search and reliable recall lanes;
- a read-only Pi extension with no shell or file-writing tools.

## Setup

```bash
cd /Users/hliu/github/marginalia/brain
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[openai,dev]'
.venv/bin/python -m research_brain --root data init
```

`OPENAI_API_KEY` is read from the environment only. Deterministic ingestion and
all dry-runs work without it.

## Reproduce the corpus

The four exact arXiv source manifests and checksums live under
`tests/fixtures/papers/`. Their source archives are deliberately not committed
because their redistribution licenses have not yet been established. Fetch and
verify the pinned revisions locally before running the full corpus gate:

```bash
.venv/bin/python scripts/fetch_paper_fixtures.py
```

```bash
.venv/bin/python -m research_brain --root data corpus load \
  tests/fixtures/papers/manifests
```

## Derived enrichment

Dry-runs never call a provider:

```bash
research --root data extract methods <document-id> --dry-run
research --root data extract math <document-id> --dry-run
research --root data index embeddings all --dry-run
```

Each live operation requires an explicit flag:

```bash
research --root data extract methods <document-id> --live
research --root data extract math <document-id> --live
research --root data index embeddings all --live
```

Repeated completed inputs use the ledger cache. `--force` creates a new immutable
run. Provider failures and invalid evidence references create failed runs and no
cards.

## Review and retrieval

```bash
research --root data object evidence <object-id>
research --root data object review <object-id> --accept --note 'Checked against TeX'

# Evidence-complete manual review queue; this does not change review state.
research --root data object list \
  --kind method_card \
  --origin AGENT_EXTRACTED \
  --review-state UNREVIEWED \
  --document <document-id> \
  --latest-extraction-only \
  --limit 50

# Exploratory: includes unreviewed cards with labels.
research --root data search 'contrastive direction'

# Reliable: source evidence plus accepted cards only.
research --root data recall 'perturbation direction' --kind method_card

# Hybrid retrieval spends one query-embedding call.
research --root data recall 'behavioral centroid translation' \
  --kind method_card --semantic-live
```

Only an explicit `object review` command changes review state; listing never
promotes cards. `--latest-extraction-only` hides older prompt/schema cohorts
from the view while preserving their immutable ledger records.

Structured filters are JSON matching `RetrievalFiltersV1`, for example:

```bash
research --root data recall 'forward pass direction' --kind method_card \
  --filters '{"gradients_required":false,"training_required":false}' \
  --semantic-live
```

Run the retrieval benchmark with:

```bash
research --root data evaluate evals/retrieval-v1.json --semantic-live
```

`--semantic-live` creates a ledgered query embedding only on the first exact
query. Later identical queries reuse the local float32 representation, including
offline calls without `--semantic-live`; corpus embeddings and lexical recall
remain available independently.

After the eight local paper archives and corpus embeddings are present, exercise
mechanism-level paraphrases without method or paper names:

```bash
research --root data corpus benchmark evals/research-moves-v1.json \
  --semantic-live --iterations 5
```

The explicit live flag only fills missing query-vector cache entries; benchmark
timings and final rankings use the offline cached path.

## Hot Frontier

Frontier objects use the existing generic research-object ledger; no separate
graph database or schema migration is required.

```bash
research --root data thread add "Principled perturbation directions" \
  --goal "Derive directions from transformer structure" \
  --constraints '["low compute","prefer no gradients"]'

research --root data observe \
  --thread <thread-id> \
  --conditions '{"model":"...","layer":12,"perturbation_norm":1.0}' \
  --evidence-refs '["obj_<experiment-result-id>"]' \
  "Direction X changed the target logit gap by 2.31."

research --root data interpret \
  --thread <thread-id> \
  --derived-from '["obj_<observation-id>"]' \
  "This is consistent with Direction X being behaviorally relevant."

research --root data tension add \
  --thread <thread-id> \
  --side-a '["obj_<hypothesis-id>"]' \
  --side-b '["obj_<experiment-result-id>"]' \
  "High activation variance but weak behavioral perturbation effect."

research --root data hypothesis add \
  --thread <thread-id> \
  --critical-unknowns '["Does the effect survive norm-matched controls?"]' \
  --what-would-strengthen '["Replication across model families"]' \
  --what-would-weaken '["No gain over random directions"]' \
  --killer-test "A powered intervention shows no effect beyond controls" \
  "A covariance eigenvector is a useful intervention direction."

research --root data question link \
  <refined-question-id> REFINES <original-question-id>

research --root data question genealogy <question-id>

research --root data transfer add \
  <question-id> <math-or-method-card-id> \
  --mapping '{"dominant_eigenvectors":{"corresponds_to":"candidate directions"}}' \
  --why-promising '["forward-only","ranked orthogonal directions"]' \
  --mismatches '["variance is observational, not necessarily causal"]' \
  --proposed-test "Compare against norm-matched random directions" \
  --thread <thread-id>

research --root data thread snapshot <thread-id>
research --root data thread snapshot <thread-id> --latest
```

Thread updates replace only the supplied frontier fields and append a
`frontier_updated` event containing the before/after state. Observations require
conditions and evidence references. Interpretations must reference Observation
objects and remain unreviewed by default. Negative Usage Episodes require a
controlled disposition, reason, and reconsideration condition.
Hypotheses record critical unknowns, strengthening and weakening evidence, and
an optional killer test. Question links are sparse, append-only relations;
`REFINES`, `SPLITS_INTO`, and `SUPERSEDED_BY` require question targets, while
`MOTIVATED_BY` and `ANSWERED_BY` may point to another research object.
Transfer hypotheses remain unreviewed proposals by default and must preserve an
explicit mapping, mismatch list, and discriminating test. Frontier snapshots are
deterministic, replaceable materializations; canonical observations and events
remain unchanged and each snapshot lists its source object IDs.

Compile a compact, deduplicated ResearchPacket with deterministic mode-specific
priorities:

```bash
research --root data context \
  --thread <thread-id> \
  --mode critique \
  --limit 8 \
  "Why might the current covariance hypothesis be wrong?"

research --root data context \
  --thread <thread-id> \
  --mode brainstorm \
  --blind-first "Independent candidate bases considered before memory retrieval" \
  "What other principled bases should we test?"
```

Available modes are `recall`, `analysis`, `critique`, `brainstorm`, and
`decision`. Brainstorm mode requires the caller's blind-first pass. Packets carry
up to 10 frontier/memory/history/tension items, compact locator-only evidence, and an
explicit corpus mismatch when nothing compatible is found. No LLM reranking is
used.

The local-only full corpus currently pins eight TeX source revisions spanning
preference optimization, model editing, activation steering, representation
reading, sparse-feature learning, refusal mediation, and forward-pass diagnostics.
After fetching the excluded archives, load and benchmark them with:

```bash
research --root data corpus load tests/fixtures/papers/manifests
research --root data corpus benchmark evals/corpus-query-v1.json --iterations 25
```

The benchmark fails unless every paper-specific target is found in the top five
and warm lexical-query p95 is at most 100 ms. It reports paper/block counts and
per-case ranks so corpus variety and speed remain inspectable.

The hybrid retrieval gate is similarly executable and returns a nonzero status
when its checked-in recall, rank, filter, review-isolation, or semantic-win
thresholds are missed:

```bash
research --root data evaluate evals/retrieval-v1.json --semantic-live
```

## Read-only Pi demo

```bash
.venv/bin/python scripts/run_pi_demo.py
```

The runner starts Pi with built-in tools, discovered extensions, skills, prompt
templates, context files, and session persistence disabled. It loads only the
three Research Brain tools and writes a structured transcript to
`artifacts/pi-demo/`. Those live transcripts are ignored by Git because they can
contain full retrieved passages and provider metadata.

The Pi recall tool accepts the same bounded access/review/revision filters as
`RetrievalFiltersV1`, returns at most eight compact hits, includes at most four
evidence excerpts per hit, and caps valid JSON output at 64,000 characters. It
does not expose live embedding generation. The adversarial cases in
`evals/pi-adversarial-v1.json` require constraint filters and explicit corpus
mismatch handling.

## Data boundary

Canonical state lives in `data/brain.sqlite3` and `data/assets/`. Model output
never becomes source evidence. An accepted agent card retains
`origin=AGENT_EXTRACTED`; its review decision is a separate append-only event.
# Named personal and shared storage

See [named spaces](docs/spaces.md) for explicit personal-corpus registration,
empty shared spaces, and the scope-validation Python contract.
