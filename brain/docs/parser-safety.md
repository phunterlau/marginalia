# Structural parser isolation and source quality

Ingestion compiles sources in a separate Python process with a 120-second wall
deadline. Timeout kills and reaps that process before ingestion returns. Parsing
happens before asset promotion or database insertion. The child receives a
minimal environment without provider credentials and does not run TeX, shell
commands, or model calls. Local-file and manifest ingestion use the same path.

Input is bounded to 50 MiB, complete archive decompression to 128 MiB, archive
enumeration to 10,000 members, and child output files to 256 MiB. The child also
has a CPU limit. Resource failures do not trigger additional PDF downloads;
include expansion is bounded to 10,000 visits, 128 nesting levels, 128 MiB expanded
text, and 100,000 structural blocks. This also bounds repeated-include expansion;
unavailable or malformed source can use the existing explicit PDF fallback.
Successfully precompiled downloaded TeX is reused instead of parsed twice.

`structural-v5` preserves source-version identity and creates a distinct parser
compilation. Its compilation diagnostics include detected/declared main TeX,
used and unused TeX members, missing and recursive includes, block/equation/comment
counts, source kind, and parser deadline. Absorption plans expose those diagnostics
alongside the pinned revision, source hash, parser identity, and unknown-license
warning. Full unused archive members remain in the canonical source asset.

Comments are masked when locating includes, without shifting source offsets.
Comment-only TeX blocks remain inspectable as source but are excluded from lexical
and semantic indexing and Method extraction. Actual repeated includes preserve
their occurrences with distinct block IDs and the same underlying source span;
recursive cycles are reported and stopped. Section context is inherited across
include boundaries. CRLF source text retains its exact spans and hashes.

This remains a structural parser, not a TeX engine: arbitrary macro expansion and
conditional compilation are not evaluated. Source evidence remains available for
human verification, and a parsed card is not scientific acceptance.

## Offline full-corpus gate

Full archives stay local. Point the test at their fixture directory without
copying them into the repository:

```sh
RESEARCH_PAPER_FIXTURES=/absolute/local/fixtures/papers \
  python -m pytest tests/test_corpus.py -q
```

The directory contains `manifests/` and `sources/`. The gate verifies pinned
revisions, repeated-ingestion identity, manifest minimum counts, every block's
exact decoded member span and hash, and the corpus retrieval benchmark. The
eight-paper local fixture set—including DPO, ROME, Refusal Direction and Logit-Gap
Steering—has passed this gate. Archives and generated validation data are not
redistributed in Git.
