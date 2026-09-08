# Arms — local research conversation control plane

Arms is independently packaged beside `brain/`. Brain owns scientific memory;
Arms owns authorization-bound conversation routing, work queues and delivery
bookkeeping. Pi will own conversational continuity. Discord will provide the
visible interaction surface. This package does not replace Pi with another LLM
conversation engine.

## Development

From the repository root, install both local packages in an external environment:

```sh
python3 -m venv /absolute/path/to/arms-env
/absolute/path/to/arms-env/bin/pip install -e ./brain -e './arms[dev]'
/absolute/path/to/arms-env/bin/python -m pytest arms/tests
```

Tests use isolated synthetic personal/shared Brain spaces and no provider calls.
Keep operational databases, source archives, backups, credentials and Pi sessions
outside the checkout. `ArmsRegistry(..., create=True)` explicitly creates a new
operational registry; ordinary opening never initializes or migrates a database.

Registry version 10 adds a tracked-message edit ledger, including edits before delivery;
version 9 added revisioned discussion outbox entries; version 8 added the discussion projection outbox and original question
channel; version 7 added immutable publication previews; version 6 added the automatic thread outbox; version 5 added pinned-paper
thread checkpoints; version 4 added source submissions; version 3 added fork staging and
version 2 added routing. For a version-1
registry, stop workers first and invoke the trusted local
`research_arms.migrations.migrate_v1_to_v2(root)` function. It refuses a held
worker lock or RUNNING claims, verifies a private SQLite backup, then migrates
transactionally. It returns the backup path (or `None` if already current).
Then run `migrate_v2_to_v3(root)`, `migrate_v3_to_v4(root)`, and
`migrate_v4_to_v5(root)`, `migrate_v5_to_v6(root)`, `migrate_v6_to_v7(root)`, then
`migrate_v7_to_v8(root)`, `migrate_v8_to_v9(root)`, then `migrate_v9_to_v10(root)` from the same module in order, starting at the registry's
current version. Each step creates its own verified backup.
Opening an old registry does not perform migrations automatically.

## Reliable research recall

The local `research_arms.feedback` interface records scoped ⭐ bookmarks, 🔥
interest, 🔬 pending deep-dive requests, and ❓ clarification flags against known
delivered answers or completed paper starters. It uses an append-only operational
event ledger; duplicate active states do not append another event. Removal
withdraws the signal. Interest scores have a 14-day half-life from activation;
duplicate add events do not refresh that timestamp. Project views aggregate only
that project's 🔥 signals from current members; personal views include only the
actor's signals within their explicitly readable spaces. No signals alter Brain
review states, create scientific memory, or dispatch model work.

Raw Gateway add/remove handlers now record these signals in serialized order,
with fresh access checks for adds. Custom emojis, bot-self reactions and unknown
targets are ignored. Guild/DM reaction intents are enabled; privileged message
content and member intents remain disabled. A stale scope cannot add a signal,
but withdrawal of a known signal may still remove it after revocation.

`/interests` shows project-only 🔥 aggregates in shared channels and the caller's
own signals in DMs. In a DM, `spaces:"project-a project-b"` explicitly includes
signals from currently accessible shared spaces; a shared command cannot attach
other spaces. Results contain references and scores, not message bodies.
Raw remove-all and remove-one-emoji events atomically withdraw matching known
signals; message deletion (including bulk deletion) also clears its signals.
These withdrawal-only paths do not depend on the former actor retaining access.
They share the reaction-handler lock so an in-flight add cannot overtake a clear.
`/interests refresh_message:<message ID>` explicitly reconciles one known answer
or paper starter in the current channel. It reads complete normal and burst user
lists for all four emojis, with at most ten 100-user pages per list and a 60-second
total bound. Malformed, repeated, excessive, or failed pages and failed access
checks leave the ledger unchanged. Registered, currently authorized users alone
can contribute; normal/burst membership is combined. Updates are atomic and
serialized against raw events. Existing active signals keep their activation time;
newly discovered signals use reconciliation time because the original reaction
time is unavailable. No reaction user names or unrelated history are retained.
Gateway ready/resume now schedules an idempotent scan of delivered answers and
completed paper starters, one target per pump iteration. Scans use existing source
authors/creators for fresh access checks, serialize against raw reaction events,
and record `COMPLETE` or `NEEDS_ATTENTION` without message bodies. No repeated retry
occurs within a pass. A restart begins a fresh pass over persisted targets; newly
created targets are handled by raw events and subsequent reconnects. If the source
author no longer has access, a currently authorized participant can use explicit
`refresh_message` recovery. Verified offline answer deletion also withdraws its
feedback through discussion reconciliation. Reaction reconciliation first verifies
the exact bot-authored message. HTTP 404 with Discord code 10008, followed by fresh
access checks, withdraws signals and marks a missing paper starter
`NEEDS_ATTENTION`; it never automatically reposts a starter or creates a new thread.
Denied access, missing channels and server errors do not establish deletion.
Replacing missing remote resources still requires explicit recovery work.

`/deep-dive message_id:<ID>` previews the exact context for an active 🔬 request.
Repeat with `confirm:true digest:<preview digest>` to approve one Pi turn in a
separate conversation. Personal work requires the owner; shared work requires a
maintainer. The preview binds source content/revision, destination, approving
principal, policy version and instructions. A source answer must have a complete,
current discussion projection and at most 12,000 characters; a paper starter
supplies its pinned source identity. No entire session is copied. Reactions alone
still never schedule work. Approval authorizes Pi synthesis/tool steps within one
turn, not ingestion, extraction, experiments, or scientific acceptance.

Approval is recorded before enqueueing. Repeating the same preview, including via
another interaction ID, resolves to the same work identity rather than another
paid turn. Interrupted setup can be retried with the original interaction identity;
policy changes fail closed. After approval, removing 🔬 withdraws the signal but
does not cancel separately approved work; use `/stop` in that conversation. The
command returns the conversation ID for `/resume` and does not switch the active
conversation automatically. This workflow is mocked, not live validated.

Current event wiring is mock-tested, not live Discord validated. Missed events can leave signals stale
until reconciliation finishes; this is eventually consistent, not an atomic
snapshot of Discord while users continue reacting.

`/frontier thread_id:<Brain research-thread obj_ID>` presents current goals,
uncertainties, pending experiments and linked research records. This is an
**exploratory** view: every record retains its own origin/review state, including
unreviewed hypotheses and negative observations. It does not create a snapshot,
run a model, or use an old snapshot as current truth. Brain research-thread IDs
are distinct from Arms/Pi conversation IDs.

Linked records use stable-ID pagination, five per page. `omitted_records` reports
the remaining count; pass `next_cursor` as `after_id` to continue. Fields are not
silently truncated; responses exceeding 66,000 bytes require the local reader.
The view is confined to the current destination space and refreshes review labels
on each request. Concurrent corpus changes may change subsequent pages; this is
a current view, not a frozen export.

`/brainstorm question` creates and selects a separate scoped Pi conversation.
Its initial turn has a durable blind-first marker: **all Brain read tools are
denied by Arms**, not merely discouraged by a prompt. Pi receives the question
and a request for independent hypotheses, uncertainties and discriminating tests.
After a draft completes, reply to its answer or use `/ask` to enter a
memory-assisted turn in that same session. Pi retains the initial draft; Arms
asks it to distinguish evidence-driven changes from retained ideas. Follow-ups
queued before any draft completes remain blind, even if processed later.
No extra second model turn is automatically scheduled. Model output remains
unreviewed discussion, not accepted scientific memory. A new command request
creates a fresh session; duplicate interaction delivery does not create another.
This workflow has mocked Pi lifecycle/tool-boundary tests, not live usefulness
validation. Optional `paper:doc_ID@vN` pins one exact source revision in the
destination space; missing revisions or private IDs used in shared channels fail
without creating a conversation. The pin is immutable for a retried request and
travels with both stages as source identity only, not paper passages or cards.
This explicit selection takes precedence over paper-thread starting-point metadata.
It is a starting point, not a restriction preventing later authorized cross-paper
retrieval.

`/save answer_message_id revision start end` previews an exact excerpt from the
current indexed answer in this channel. Obtain the revision from `/discussed`.
Offsets are zero-based decoded characters, end-exclusive, selecting 1–8,000
characters. Repeat with `confirm:true digest:<preview digest>` to retain it.
Shared saves require a maintainer; personal saves require the owner. Stale or
unfinished projections and wrong-channel answers fail closed. The confirmation
rechecks the exact revision and digest atomically with note creation; duplicate
confirmations return the same note without another audit event.

Saved excerpts are `AGENT_INTERPRETED` and `UNREVIEWED`, never observed experiments,
accepted memory, or automatically verified paper evidence. Their provenance
retains the discussion revision/digest, selected span, author attribution, answer
link and Pi entry ID—not unrelated messages or session files. Original discussion
records remain unchanged. Later edits/deletions of the discussion do not silently
rewrite or delete this explicitly saved historical excerpt. Source citations in
the text remain unvalidated text, not manufactured evidence links. Structured
proposal editing and claim-level evidence annotations are not part of this command.

`/recall question` searches only the current destination's Brain space, without
starting or resuming Pi. It returns up to three source/accepted-record hits with
their origin, review state, source locators, evidence and structured constraints.
Shared queries never attach the requester's personal memory. `/discussed` remains
a separate search of bot-directed exchanges, not scientific memory.

Questions are bounded to 2,000 characters. Results use the existing same-command
JSON attachment delivery and a 66,000-byte ceiling; oversized results fail visibly
instead of silently trimming evidence. Narrow the query or use the local reader.
Retrieval makes no paid query-embedding call: an existing cached query vector can
contribute semantic results; otherwise retrieval is lexical. Empty results do not
establish that no relevant method exists. Review status is read afresh, and
acceptance never changes agent origin. Transport metadata paths are omitted.

`/compare papers:"doc_ID@v1 doc_OTHER@v2" question:"Which constraints differ?"`
returns a read-only comparison dossier for 2–4 distinct exact revision selections.
The same paper at two revisions is allowed. Each group records its space,
document/version identity and revision, with up to three reliable evidence hits
and their constraints. All pins must resolve in the destination before retrieval;
there is no latest-revision or cross-space fallback. Empty groups explicitly
indicate a retrieval/corpus mismatch. Queries are limited to 2,000 characters and
the complete response to 66,000 bytes, without silent evidence truncation.
This is an evidence comparison, not a generated scientific judgment or a saved
Pi conversation. No model call, review decision, or Brain mutation occurs.

## Interrupted worker recovery

Failed discussion-index writes have a separate trusted-local recovery command:

```sh
python -m research_arms.discussion_recovery --root /absolute/arms-data \
  --spaces-root /absolute/space-registry --space project
python -m research_arms.discussion_recovery --root /absolute/arms-data \
  --spaces-root /absolute/space-registry --space project --retry 'TURN_ID:REVISION'
```

Inspection returns at most 20 failed-job summaries (`--limit` accepts 1–100),
without message content. Retry queues only an exact latest `NEEDS_ATTENTION`
projection in the named space and records an operator audit event. It does not
call Pi, spend tokens, resend Discord messages, or modify the original turn.
The normal Gateway worker performs the index write with fresh authorization;
revoked scopes cannot reintroduce content. Privacy retractions may still remove
search visibility in their original space. Brain's immutable revision identity
makes retry safe when its write succeeded but the Arms completion checkpoint was
lost. RUNNING jobs require the stopped-worker recovery below first. This command
does not reconcile edits missed while offline or backfill old deliveries.

Stop the Gateway and verify its orphaned Pi/worker processes have also stopped.
Then run this trusted local command (never exposed to Discord or Pi):

```sh
python -m research_arms.recovery --root /absolute/private/arms-runtime \
  --spaces-root /absolute/private/brain-spaces --confirm-workers-stopped
```

It refuses a held supervisor lock, creates a private integrity-checked SQLite
backup, and atomically quarantines interrupted conversations, source submissions,
thread jobs, thread-creation checkpoints and partial forks. Uncertain message
deliveries become UNKNOWN. It never retries a provider call or Discord write,
deletes assets, or labels partial work complete. Brain paid-job ledgers are
unchanged and need their own explicit inspection. The JSON result reports counts
and the local backup path. Repeated recovery adds no duplicate quarantine events.
A free lock alone does not prove child processes are stopped; that remains an
explicit operator verification. A recovered paper checkpoint can subsequently use
`/paper reconcile` if the remote starter and thread already exist and verify.

## Current library contract

`research_arms.ArmsRegistry` uses only Brain's public `SpaceRegistry` interface.
Trusted local administration registers Discord-user/principal/personal-space
mappings and binds guild channels to shared spaces. These administrative methods
are **not remote command handlers** and must not be exposed to bot users.

An authenticated transport creates conversations with exact channel/thread IDs.
It must verify real guild/thread/parent identity and Discord permissions itself;
the registry cannot authenticate caller-supplied Discord IDs. Shared channels
cannot bind personal spaces. A DM can attach explicitly selected shared spaces.
Each conversation has a permanent visibility digest and dedicated Pi session ID.
Different authorized authors share the same shared conversation, while every
turn stores its own acting principal and immutable ContextScope.

`select_conversation` explicitly selects an authorized OPEN conversation for a
DM user/channel or shared channel. `resolve_conversation` returns that binding,
its audience and read spaces; it never guesses from titles or recency. Replies
to delivered bot answers resolve their exact conversation and quoted-turn
anchor without changing the active DM selection. Missing anchors fail closed.
The Gateway authenticates Discord events and routes replies to registered paper
starters into their dedicated threads, checking access to both destinations.
`/new`, `/resume`, and `/session` expose explicit conversation selection; incoming
reply routing uses the stored bindings rather than a global latest session.

`enqueue` retains only explicitly forwarded bot-directed turns. Duplicate
message delivery is idempotent; changed message content requires a separate edit
workflow. Reply anchors must be completed turns in the same conversation.
`claim` atomically enforces one active turn per conversation and two globally.
No process is started by claiming. `save_answer` commits the answer and pending
delivery record together. Reopening preserves exact identities; RUNNING work is
never automatically replayed after a crash.

`read` is a worker capability bound to a persisted running turn, not a client-
selected scope. It exposes bounded source/object/search/recall operations only,
with no provider calls, arbitrary filesystem paths or Brain mutations. It checks
current authorization both before and after retrieval. Missing and forbidden
resources share an unavailable error. Backend asset/session paths are stripped
from structured results. Policy changes invalidate contaminated scopes;
`revoke_stale` quarantines their pending work/deliveries and returns the session
IDs a future supervisor must abort. It does not itself terminate processes.

## Pi RPC and delivery primitives

`research_arms.pi_rpc.PiRPC` launches Pi without a shell, uses an exact session
UUID and backend session directory, disables built-in tools and implicit
extensions/skills/templates/context files, and disables automatic retries. This
client optionally loads exactly four scope-bound research tools through
`ToolBridge` and the bundled extension. Environment API keys are excluded; Pi's
configured login is the intended authentication route for future synthesis.

Responses are correlated by request ID. Frames use bounded LF-only JSONL.
Prompts wait for `agent_settled`, not the earlier `agent_end` event. Timeouts
terminate the client and require preserving uncertainty, never automatic replay.
`abort` clears Pi's queue first. Raw bash/session-switch/export RPC commands are
unavailable. Tests use synthetic subprocesses. A native installed-Pi startup
check also succeeded with zero model prompts and an isolated configuration.

The bridge uses a private local Unix socket with a per-turn rotating capability.
Only the supervisor binds a running turn; model arguments cannot choose a
principal, session, turn or filesystem path. The extension exposes only
`research_recall`, `research_evidence`, `research_object`, and `research_discussed`, each requiring a
space identity. Pi startup checks the exact active tool set via its supported
RPC notification channel (ordinary stdout is intercepted by Pi). Run the
optional installed-Pi startup test with `ARMS_TEST_PI=/absolute/path/to/pi`.
Socket tests verify shared/private isolation and revocation. Native startup
verification proves loading, not a model-driven tool call or a complete turn.

`research_arms.worker.Supervisor` connects claimed turns to Pi and stores finished
answers in the registry/outbox. It holds a single-supervisor file lock, bounds
the pool to two processes, reuses exact conversation sessions, and partitions
session directories by space. Call `run_once()` to process one queued turn and
`maintain()` periodically to retire ten-minute-idle or revoked workers. Always
close the supervisor. No background loop or service is installed automatically.
Existing RUNNING claims fail startup closed until prior processes have been
verified stopped and explicit recovery has occurred.

Prompts contain the current immutable scope, author, question, and an optional
bounded quoted reply anchor with an omission count. Dispatch is audited before
prompting. Only a newly returned, normally completed assistant message becomes
an answer; errors, truncation and unresolved tool calls do not. Interrupted turns
quarantine their conversation and queued follow-ups without automatic replay.
Synthetic worker tests cover reuse, concurrency, revocation during generation,
idle retirement and answer/outbox persistence. Live synthesis and process-kill
recovery validation remain open; these are not proven by native startup tests.

`Supervisor.stop(conversation, discord_user, channel_id=..., guild_id=...)`
authorizes the caller and destination, cancels queued turns, invalidates active
turn capabilities, clears Pi's queue, aborts and closes the worker. A late answer
cannot enter the delivery outbox. An interrupted conversation is marked STOPPED
and cannot silently resume. `/fork` can branch from an exact completed turn;
`/fork-recover` can adopt a verified partial fork as described below. Neither
operation replays an interrupted model call.
Stopping an idle conversation cancels its queue without invalidating completed
history. Stop cannot undo provider calls or remote sends already dispatched.

`session_fork.fork_completed_session` is a trusted local branching primitive,
not a remote authorization API. It uses Pi's session SDK to retain the branch
through an exact completed assistant entry. Pi's RPC `fork` instead targets a
user message and is not used for this operation. The installed-SDK test verifies
that later canary history is excluded and the source file is unchanged, without
model calls. The coordinator below authorizes both scopes, retires the source
worker, and stages the destination binding. Partial filesystem output requires
explicit verified recovery rather than automatic recreation.
Do not expose the helper's filesystem arguments to Discord or model tools.

`forks.fork_conversation(supervisor, ...)` provides the same-audience coordinator.
It authorizes an exact completed turn, retires the source worker, persists a
FORKING destination before creating the file, and activates it only after a fresh
authorization check. The request ID is idempotent; partial failures remain
NEEDS_ATTENTION and are never automatically replayed. Scope changes require a
fresh conversation, not copying old context into a narrower or shared audience.
The first child turn verifies that its inherited answer exists in Pi history.
Native SDK/coordinator tests exclude future canary history without model calls.
The Discord `/fork answer_message_id:... name:...` command uses this coordinator
and selects the resulting conversation. The answer must have been delivered in
the current channel/DM; private history cannot be branched into a shared channel.
Discord access is refreshed before branching and before activation. A transport
permission change quarantines the partial fork. The interaction ID makes retries
idempotent. `/fork-recover request_id:<original interaction ID>` can adopt an
existing verified partial fork for the original requesting actor in the same
channel/audience. Failed `/fork` responses include that request ID. In-flight
operations first require stopped-worker recovery; RUNNING work cannot be adopted.
Exactly one candidate must exist, with its session filename matching its header.
Ambiguous, missing, modified, or scope-revoked candidates remain quarantined.
Recovery retires idle workers, rechecks access, and atomically updates the fork
and conversation checkpoints with one audit event. A repeated recovery returns
the same conversation; use `/resume` to select it. No source/candidate file is
rewritten and no new branch or model turn is created.
The trusted local `session_fork.verify_completed_fork` primitive can now verify an
existing SDK-created candidate without opening it through the SDK or rewriting
either file. It checks exact source/session identity, the completed assistant
entry's parent chain, all retained content, rechained labels, and absence of
future entries. Files are bounded to 20 MiB; unknown session versions fail closed
rather than being implicitly migrated. Native installed-Pi tests cover labeled
and unlabeled forks and altered-content rejection. This verifier does not itself
activate a quarantined conversation; `/fork-recover` performs the authorized adoption.

Branching uses the local Node executable and `index.js` beside the resolved Pi
executable by default. Backend administrators can override these with
`--fork-node` and `--fork-sdk`; Discord callers cannot supply filesystem paths.

Delivery claims persist SENDING before external dispatch and distinguish UNKNOWN
from DELIVERED. Confirmations are idempotent; uncertain sends require explicit
remote reconciliation and are never automatically resent. `validate_delivery`
checks current scope before a future adapter sends. These methods send nothing
themselves; the adapter must enforce actual audience permissions and suppress
mentions. Recovery requires prior verification that old workers are stopped,
quarantines interrupted conversations, and marks interrupted sends UNKNOWN.

## Discord IO primitives

Install `arms[discord]` for the optional HTTP client. `discord_io` accepts only
bounded UTF-8 `.txt`/`.md` attachments from Discord CDN attachment URLs, with no
redirects and no bot credentials on download requests. Call it only after
authenticating and authorizing the incoming event. Combined questions are capped
at 20,000 characters; declared and streamed bytes are bounded separately.

`DiscordSender` posts one persisted answer using a stable nonce, disabled
mentions and suppressed embeds. Longer answers become an exact Markdown file.
It requires a transport-provided current Discord audience/permission check;
local Brain membership alone is insufficient. Every failed or ambiguous send
remains UNKNOWN without automatic retry. `reconcile` reads one exact remote
message and checks author, channel and nonce before confirming it, never resending.
Use `/delivery-recover turn_id:... answer_message_id:...` in the original
destination to recover an UNKNOWN delivery of your own turn. It cannot adopt
an active SENDING delivery, another author's turn, or an answer in another
channel. Current access is checked before and after the remote read. Missing
or unverifiable messages remain unresolved; the command never resends them.
Discord nonce deduplication is time-limited, not a substitute for durable outbox
state. See the [Discord message API](https://docs.discord.com/developers/resources/message).
Tests use fake HTTP responses; no live Discord send has been performed.

`DiscordAccess` resolves current DM recipients, channel/guild identity, configured
parent project, roles, member overrides, and thread access from fresh REST reads.
Group DMs, mismatched parents, archived/locked threads, and unavailable permissions
fail closed. Private threads still map to the parent's shared Brain space.
Pass `authorize_turn=access.authorize_turn` to the supervisor for Discord work:
checks run before launch/dispatch, around tool results, before answer persistence,
and during maintenance. The Gateway adapter must also authorize incoming events
and supply the sender's audience check; omitting the hook is only for trusted
local workflows. See [Discord permissions](https://docs.discord.com/developers/topics/permissions)
and [thread permissions](https://docs.discord.com/developers/topics/threads).

## Remaining integration

The foreground [Discord pilot setup](DISCORD_SETUP.md) now exposes `/new`,
`/resume`, `/session`, `/space`, `/ask` and `/stop` through the Gateway. It has no
HTTP listener and synchronizes commands only with an explicit flag. Construction
and command-handler tests are offline; no live bot connection has been validated.
Bot-directed DMs, mentions and mapped answer replies are accepted; unrelated
channel chatter is ignored. Paper-starter routing and paper/feedback commands
remain pending.

Read-only `/paper status`, `/paper brief`, `/paper cards` and `/paper evidence`
now accept exact document/block IDs in the destination space without starting Pi.
Brief/status report source metadata, not a generated scientific summary or proof
that an absorption job completed. Card previews preserve review labels; evidence
includes its locator and explicit text omission information. Larger command
results are attached as JSON. `/paper job` and `/paper approve` inspect and approve
existing scoped jobs for owners/maintainers, with an exact digest and explicit
confirmation. The command only queues work; it never calls a provider itself.
`/paper add` durably queues an owner/maintainer's source request with bounded
spending limits; `/paper submission` reports the resulting approval-job ID.
One background source task runs at a time, without model calls. Failed or
interrupted submissions require attention rather than automatic replay. Graceful
shutdown joins source ingestion before releasing worker ownership. Existing
RUNNING source rows after a restart need local reconciliation and block new
source claims.

`/paper thread job_id:...` creates a shared paper starter and public thread from
an existing source job. It requires current maintainer access and the bot's Create
Public Threads permission. The exact paper revision gets a dedicated session;
repeated requests reuse its thread, while different revisions stay separate.
Replies to the starter route into the paper thread without switching the parent
channel's selected conversation. Pi receives the space, document and pinned
revision as its starting point. Message/thread dispatches have durable checkpoints;
uncertain outcomes require reconciliation and are never automatically resent.
`/paper reconcile job_id:... starter_message_id:...` recovers a settled uncertain
operation by reading the exact existing starter and thread. It verifies the
bot author, request nonce, parent, guild and thread identity, then restores the
local session binding without sending or creating anything on Discord. Replays
are idempotent. Missing resources or a missing nonce stay unresolved; Discord
documents nonce as optional, so this is not guaranteed recovery for every remote
message. In-flight checkpoints after process termination require trusted offline
recovery first. A conflicting preexisting session also requires local inspection.
Recovery of missing remote resources remains pending.
This command creates a starter but does not pin the Discord message.

New shared Gateway submissions automatically enqueue thread work in the same
transaction that records source completion. One independent thread worker uses
the submitter's original scope and fresh Discord/maintainer checks. DM submissions
never queue shared threads. Thread failure does not invalidate the source or
approve/replay paid work. `/paper submission` reports the thread job separately.
Repeated paper revisions reuse completed threads. Uncertain operations are not
retried, and stale RUNNING thread jobs require local recovery. Graceful shutdown
joins an in-flight thread operation before releasing worker ownership. Migration
does not backfill historical submissions or unexpectedly post their papers.

`--run-approved-absorption` opts the Gateway into background paid processing of
exact, already approved submission jobs. It is off by default. The worker checks
both source/approver membership and current Discord access at provider-dispatch
boundaries, and cannot claim an unrelated queued job in the same Brain database.
Shutdown prevents further dispatches and joins in-flight work. Provider calls
remain direct Brain work, not Pi tools. Synthetic tests cover the integration;
no live absorption validation is claimed.

`ScopedAbsorption` is the maintainer-only service foundation for those mutations.
It binds an immutable authorization context into the paid-plan digest and checks
it again before approval and provider dispatch. A plain authorization-unaware
worker refuses scoped jobs. Shared members cannot submit or approve absorption;
personal operations require the owner. Submission requires explicit spending
limits and does not itself run model work. No live scoped absorption has
been performed through this service.

`/card show object_id:...` returns a Method/Math card with its exact stored evidence
and `updated_at` version. `/card review object_id:... decision:... expected_version:...`
records an explicit human judgment in the current space; shared maintainers and
personal owners can write, while ordinary shared members can inspect. Dispute and
rejection require a note (maximum 4000 characters). Acceptance means faithful
representation of the cited source and its qualifications, not universal truth
or suitability. It makes the card eligible for reliable recall without changing
agent origin or content. Reviews require evidence, use atomic version checks, and
append Brain's audit event with the Discord actor. Stale submissions must reload;
they are not automatically retried. Oversized cards require the local workbench.
Pi tools remain read-only, and no real cards have been accepted by automated tests.

`publication.Publications` is the local consent and execution interface.
`prepare` selects the owner's plain curated
notes and/or evidence blocks identifying pinned arXiv papers, and creates an
immutable bounded preview. Linked evidence is included explicitly using bundle
aliases; private object/block/session IDs and filesystem paths are not exported.
Structured notes and unresolved private textual references must first be curated.
The exact text, evidence, source revision/hash/license and destination audience
are shown before consent. Destination maintainers cannot inspect a PREVIEW.
`decide(..., action="consent")` requires its source owner and exact digest;
`action="approve"` additionally requires a destination maintainer. The owner may
cancel before execution. Policy changes invalidate pending approvals. Every
decision is audited; approval alone still creates **zero destination records**.
`execute(actor, publication_id, digest)` requires current destination-maintainer
access, owner consent, destination approval and unchanged policy. It copies only
selected hash-verified local arXiv source assets, recompiles them in the destination,
and resolves evidence by exact text, equation, member, line/character/page locators
and source hash. Parser mismatches fail closed. Notes preserve origin and remain
UNREVIEWED; all notes and a destination receipt commit atomically. Approved source
papers may be searchable earlier, but partial publication is not marked complete.
No model calls, downloads, private session files or unrelated memory are copied.
An uncertain execution requires explicit `retry=True` after inspection; a receipt
reconciles a lost Arms completion without duplicating destination records. Recovery
quarantines interrupted publications. Publication does not imply scientific acceptance.

Discord exposes `/publish prepare`, `show`, `consent`, `approve`, `cancel`, and
`run`. Prepare/consent/cancel are owner-only DM operations. Preparation identifies
a configured destination channel, checks the owner's Discord access there, and
accepts at most ten whitespace-separated note IDs and ten paper-evidence IDs.
Before consent, even the owner cannot show that preview in a shared channel.
After consent, destination maintainers can inspect the approved bundle in its
matching project channel. Approve/run are restricted to that destination; each
decision requires the exact digest and `confirm:true`. An inspected uncertain
execution additionally requires `retry:true`. Complete previews up to 100 KB are
attached as JSON without truncating consented text. Pi has none of these tools.
Only one publication execution runs at once; shutdown joins the local writer.
Execution rechecks the operator, source owner and approver's current Discord access
between source copies and before note publication. Discord controls are mock-tested;
live sharing validation remains pending.

`/discussed question:...` searches the current destination's recorded discussion,
not the owner's personal history from a shared channel. Confirmed delivery queues
an immutable exchange snapshot in the same Arms transaction; a separate worker
projects it into the original writable Brain space after fresh authorization.
Unknown/undelivered answers are not indexed. Projection never calls a provider.
Brain's idempotent revision ledger handles duplicate records; failed/interrupted
projection requires attention rather than silently replaying. Shutdown joins the
writer and local recovery quarantines RUNNING projection jobs. Original question
channels are preserved for paper-starter replies; unknown legacy locations are
not invented. Migration does not backfill historical deliveries. Brain schema 003
must be migrated explicitly before projection. Historical
backfill remains pending; failed latest projections can be explicitly retried
with the trusted local discussion-recovery command described above. Pi can use `research_discussed` with an
explicit permitted space, a query of at most 2000 characters, and at most three
results. The same turn-bound credentials, revocation checks and output limits
apply. Results label discussion as non-scientific memory and identify question
authors separately from assistant answers. Scientific recall is unchanged.

This is a partial pilot, **not a validated live deployment**. Complete Discord
recovery and multi-user privacy gates remain open. Discussion and reaction
reconciliation are integrated and mock-tested, not live-validated.

Authenticated raw single/bulk Discord deletion events match only already tracked
message IDs and their exact guild/channel. They append idempotent tombstones to
the discussion outbox; unknown casual messages are not retained. Deleting either
side removes the exchange from discussion search after projection, including when
the question was deleted before answer confirmation. Deletion retractions remain
effective after membership changes because they only reduce search visibility
in the original space. Original turns, revision history and Pi context are not
erased, and nothing is replayed to Pi. Reconnect reconciliation checks known
message identities for offline deletion; legacy unknown question transports are
skipped. Deletion is not a promise of provider erasure.

Raw tracked-message edit events record an unavailable edit, then fetch
only the exact tracked message after fresh access checks. Author/channel identity
and edit timestamp must verify before the current content is reindexed. Question
attachments use the existing bounded UTF-8 reader. Bot-answer attachments must be
exactly one `answer.md`, with matching declared/actual length, valid UTF-8 and a
200,000-byte maximum. Only Discord CDN attachment URLs are fetched, through the
credential-free bounded downloader; delivery-wrapper text is never substituted.
Discussion results explain that the assistant answered an earlier question version.
Duplicate/stale edits do not overwrite a newer
revision, and edits cannot undo deletion. Failed fetches or oversized edits remove
stale content from search after projection. Pi turns and session history are
never rewritten or replayed. Before delivery, edits are retained in an immutable
ledger and applied atomically to the initial discussion projection at confirmation;
deletion markers still take precedence.
New turns also record whether their question came from a Discord message or a
slash-command interaction. An interaction ID is not a message ID: `/ask` questions
remain searchable but have no source-message URL and cannot be targeted by raw
message edits/deletions. Their delivered answers still have ordinary message links.
Transport markers are written atomically with the turn; historical records are
not guessed or rewritten. Reconnect scans only fetch question IDs
with an explicit message marker, not legacy unknown or interaction IDs.

Gateway ready/resume starts a bounded scan of existing tracked turns, one turn
per pump iteration, with at most its question and confirmed answer checked.
It uses exact-message GETs, not channel-history downloads. Edited content goes
through the same verified attachment/text reader. Only HTTP 404 plus Discord's
`10008` (Unknown Message), with fresh access checks before and after, creates a
deletion marker; missing channels, denied access, rate limits and server errors
remain `NEEDS_ATTENTION` outcomes. Outcomes are audited without message content.
See [Discord error codes](https://docs.discord.com/developers/topics/opcodes-and-status-codes).
Each message check has a 60-second bound. A scan snapshots the turn high-water mark,
coalesces reconnect requests, and restarts idempotently after process restart.
Failures are not retried in a tight loop; the next reconnect checks them again.
Reconciliation is eventually consistent: search may still expose stale content
until the scan and discussion projection complete. Historical unknown question
transports are deliberately skipped. This is offline mock-tested, not a completed
live reconnect/privacy gate.
The delivery and permissions paths are mock-tested, not live-tested. There is no
HTTP listener, installed daemon or LaunchAgent.

Live use still requires the absorption validation gate, user-provided Discord
setup, process-kill/reconnect recovery validation and controlled multi-user
testing. Synthetic canary tests are not substitutes for those gates.
