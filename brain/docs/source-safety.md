# Source and generation safety

Source resolution accepts exact arXiv identifier paths, including pinned `/src/`
and legacy identifiers. Unversioned arXiv requests fail if revision discovery
fails; they no longer silently download an unpinned artifact. Redirects from
arXiv may not leave its allowed hosts or change an explicitly pinned identity.
The generic, trusted local `ingest` command still accepts non-arXiv URLs.

Downloads are limited to 50 MiB. Complete gzip decompression is bounded to
128 MiB, including unused archive members. Archive enumeration is limited to
10,000 members. Size-limit failures do not trigger PDF fallback. Parsing occurs
before asset promotion. A subsequent database failure can leave an orphan
content-addressed file; it cannot leave a source row pointing to a missing asset.
Do not automatically delete orphan assets. Maintenance reports orphan/staging
files without removing them. A [120-second parser subprocess deadline](parser-safety.md)
is now enforced.

Byte-identical explicit paper revisions have separate identities. Reingestion
reuses an existing matching revision/hash identity, preserving historical IDs.
Newly initialized/migrated stores use WAL; connections enable foreign keys and a
five-second busy timeout. Existing stores are not silently switched to WAL when
opened through the named-space interface.

Provider dispatch is written before each extraction or embedding call. An
interrupted `running` generation blocks implicit re-execution. Failed runs get
new IDs on explicit retry, while completed identical work remains cacheable.
Completed attempt outcomes cannot be overwritten. This is not yet a full job
lease/recovery system: inspection and administrative reconciliation of interrupted
runs are still required before retry.

Extraction retries only explicit 429/5xx errors, at most twice. Unknown provider
exceptions are not presumed safe to replay. The Responses adapter disables SDK
retries and caps each call at 16,384 output tokens with a 120-second client timeout.
The ceiling includes reasoning tokens per [OpenAI's reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).
The generation request records this cap and uses a new prompt/configuration
version so older cached output cannot masquerade as bounded work. These per-call
limits are **not** a total job budget or spending approval mechanism.

No change here promotes generated cards to accepted scientific memory. No live
provider validation is implied by the synthetic failure and adapter tests.
