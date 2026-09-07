# Approved arXiv absorption

Absorption is a durable workflow in a **registered space**, independent of
Discord. [Register the existing corpus as personal first](spaces.md). Never
register a personal corpus as shared just to make it available to a bot.

```sh
research --space personal absorb https://arxiv.org/abs/2506.24056 \
  --max-calls 32 --max-reserved-tokens 2000000

# Inspect the JSON plan, source quality, pinned revision and exact digest.
research --space personal jobs show JOB_ID
research --space personal jobs approve JOB_ID --plan-digest DIGEST --live
research --space personal worker --once
research --space personal jobs show JOB_ID
```

Use `--registry /absolute/registry` or `RESEARCH_SPACE_REGISTRY` to select a registry.
All output is JSON. A missing/incompatible Brain or a job database registered to
another space fails closed. No implicit migration occurs.

The first command downloads and ingests source, then stops at `WAITING_APPROVAL`.
It never contacts a model. An unversioned arXiv URL must resolve to a pinned
revision. The plan binds its space, document, compilation, source/block digest,
tasks, model, reasoning effort, prompt/schema contract, and budget. Methods, Math
when equations exist, and embeddings are the default tasks. Embeddings cover only
the pinned compilation and the cards returned by these extraction tasks, not
other documents or generic objects in the space.

Source passages are immediately searchable. `COMPLETE` means all selected
processing finished; **it does not mean scientifically reviewed**. Cards retain
`AGENT_EXTRACTED` / `UNREVIEWED` until a human reviews them. Use the existing
review CLI or memory workbench for acceptance and reliable-recall eligibility.

## Spending and recovery

The worker uses the direct OpenAI endpoint, the environment's `OPENAI_API_KEY`,
and the approved models. No key is stored in a plan. Alternate `OPENAI_BASE_URL`
endpoints are refused. Default model settings are `gpt-5.6-luna`, `medium`, and
`text-embedding-3-small`; changing environment model settings after approval
does not change the frozen job.

Every dispatch reserves one call and a conservative text-token allowance:
ASCII-escaped JSON input bytes + 4,096 protocol allowance + 16,384 output tokens
for extraction (zero output tokens for embedding). This is a conservative
admission budget, **not a dollar estimate or tokenizer-based usage estimate**.
Returned provider usage is retained separately. Reservations are not refunded on
failure or retry. Missing limits never mean unlimited spending. Every 429/5xx
retry consumes another reservation; at most two such retries occur per extraction
chunk. Unknown transport outcomes are not automatically replayed.

The job database uses WAL, foreign keys and atomic claims. An OS worker lock
allows one foreground worker per space; it is released on process death. On the
next worker invocation, interrupted jobs become `NEEDS_ATTENTION`, not queued.
Dispatch and returned output are persisted before results are validated. Source
and card content are canonical in `brain.sqlite3`; job checkpoints and approvals
are in `jobs.sqlite3`. A canonical commit followed by a lost checkpoint reuses
completed work through the generation cache. The workflow does not assume a
transaction spans both databases.

```sh
research --space personal jobs list --limit 50 --offset 0
research --space personal jobs cancel JOB_ID
research --space personal jobs retry JOB_ID

# Only after inspecting an uncertain/interrupted attempt:
research --space personal jobs retry JOB_ID --ack-uncertain
# Retry returns to WAITING_APPROVAL; approve the digest again before working.
```

Cancellation prevents future dispatch, but cannot undo a call already sent.
An uncertain retry may incur another charge; acknowledgement is recorded.
Successful task checkpoints survive retries. Budget reservations remain
cumulative. If the budget is insufficient, submit a new explicitly reviewed plan
with different limits rather than silently increasing the old approval.

Extraction chunks respect section boundaries and the 180,000 decoded-character
JSON limit. Large prose is split with source-block IDs and relative excerpt
offsets; exact source evidence remains unchanged. Large canonical equations fail
visibly rather than being truncated. Equation context may span several bounded
calls, with the exact equation repeated alongside complete prose excerpts.

## Validation boundaries

The offline suite includes real subprocess termination, cancellation, retry
ceilings, stale approvals, wrong-space jobs, malformed outputs, transactional
card rollback, and canonical-commit/checkpoint failure. These tests use synthetic
providers and do not establish live API behavior or scientific usefulness.

Before unattended use, the remaining backend gates are consistent backup/restore,
explicit backed-up migration, staging reconciliation, a parser process deadline,
the full local-corpus failure matrix, and an explicitly approved live absorption.
This command does not yet install a daemon, expose Discord, or start Pi.
