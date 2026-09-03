# Research Brain

Research Brain is an evidence-first, headless research memory for Pi and other
agent clients. Original sources and exact locators remain canonical; extraction,
embeddings, and generated connections are derived and explicitly labeled.

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

# Exploratory: includes unreviewed cards with labels.
research --root data search 'contrastive direction'

# Reliable: source evidence plus accepted cards only.
research --root data recall 'perturbation direction' --kind method_card

# Hybrid retrieval spends one query-embedding call.
research --root data recall 'behavioral centroid translation' \
  --kind method_card --semantic-live
```

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

## Read-only Pi demo

```bash
.venv/bin/python scripts/run_pi_demo.py
```

The runner starts Pi with built-in tools, discovered extensions, skills, prompt
templates, context files, and session persistence disabled. It loads only the
three Research Brain tools and writes a structured transcript to
`artifacts/pi-demo/`. Those live transcripts are ignored by Git because they can
contain full retrieved passages and provider metadata.

## Data boundary

Canonical state lives in `data/brain.sqlite3` and `data/assets/`. Model output
never becomes source evidence. An accepted agent card retains
`origin=AGENT_EXTRACTED`; its review decision is a separate append-only event.
