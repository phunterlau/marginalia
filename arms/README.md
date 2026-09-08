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

Registry version 3 adds durable fork staging; version 2 added routing. For a version-1
registry, stop workers first and invoke the trusted local
`research_arms.migrations.migrate_v1_to_v2(root)` function. It refuses a held
worker lock or RUNNING claims, verifies a private SQLite backup, then migrates
transactionally. It returns the backup path (or `None` if already current).
Then run `migrate_v2_to_v3(root)` from the same module; existing version-2
registries need only this second step. Each step creates its own verified backup.
Opening an old registry does not perform either operation automatically.

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
Transport handlers must display active selections, authenticate Discord events,
and route paper-starter replies into their paper threads; starter mapping is
still pending. These library methods are not installed Discord commands.

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
client optionally loads exactly three scope-bound research tools through
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
`research_recall`, `research_evidence`, and `research_object`, each requiring a
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
and cannot silently resume; explicit recovery/fork controls are still pending.
Stopping an idle conversation cancels its queue without invalidating completed
history. Stop cannot undo provider calls or remote sends already dispatched.

`session_fork.fork_completed_session` is a trusted local branching primitive,
not a remote authorization API. It uses Pi's session SDK to retain the branch
through an exact completed assistant entry. Pi's RPC `fork` instead targets a
user message and is not used for this operation. The installed-SDK test verifies
that later canary history is excluded and the source file is unchanged, without
model calls. The future coordinator must authorize both scopes, stop the source
worker, stage the destination binding, and reconcile partial filesystem output.
Do not expose the helper's filesystem arguments to Discord or model tools.

`forks.fork_conversation(supervisor, ...)` provides the same-audience coordinator.
It authorizes an exact completed turn, retires the source worker, persists a
FORKING destination before creating the file, and activates it only after a fresh
authorization check. The request ID is idempotent; partial failures remain
NEEDS_ATTENTION and are never automatically replayed. Scope changes require a
fresh conversation, not copying old context into a narrower or shared audience.
The first child turn verifies that its inherited answer exists in Pi history.
Native SDK/coordinator tests exclude future canary history without model calls.
Discord handlers and user-facing partial-fork reconciliation are still pending.

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
Discord nonce deduplication is time-limited, not a substitute for durable outbox
state. See the [Discord message API](https://docs.discord.com/developers/resources/message).
Tests use fake HTTP responses; no live Discord send has been performed.

## Remaining integration

This is an offline control-plane foundation, **not a running Discord bot**.
Gateway authentication/handlers, default active-DM and paper-thread routing,
forks, Discord stop handlers and recovery controls, the background supervisor loop,
actual delivery and remote reconciliation, publication consent, scientific reviews,
discussion search and reactions are not yet connected. Outbox insertion is
tested, not external delivery. There is no HTTP listener, daemon or LaunchAgent.

Before exposing any transport, add process ownership/recovery and revalidate
authorization at the actual outbound-send boundary. The worker's turn capability
must be injected server-side, never accepted from a Discord user or Pi tool
argument. Session file locations must be separately rooted by space. Live use
also requires the absorption validation gate, user-provided Discord setup and
controlled multi-user testing. Synthetic canary tests are not that live gate.
