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
`ToolBridge` and the bundled extension. A supervisor still needs to connect the
worker lifecycle. Environment API keys are excluded; Pi's
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

Delivery claims persist SENDING before external dispatch and distinguish UNKNOWN
from DELIVERED. Confirmations are idempotent; uncertain sends require explicit
remote reconciliation and are never automatically resent. `validate_delivery`
checks current scope before a future adapter sends. These methods send nothing
themselves; the adapter must enforce actual audience permissions and suppress
mentions. Recovery requires prior verification that old workers are stopped,
quarantines interrupted conversations, and marks interrupted sends UNKNOWN.

## Boundaries still to implement

This is an offline control-plane foundation, **not a running Discord bot**.
Gateway authentication/handlers, default active-DM and paper-thread routing,
forks, the Pi process pool, user-facing stop/recovery,
actual delivery and remote reconciliation, publication consent, scientific reviews,
discussion search and reactions are not yet connected. Outbox insertion is
tested, not external delivery. There is no HTTP listener, daemon or LaunchAgent.

Before exposing any transport, add process ownership/recovery and revalidate
authorization at the actual outbound-send boundary. The worker's turn capability
must be injected server-side, never accepted from a Discord user or Pi tool
argument. Session file locations must be separately rooted by space. Live use
also requires the absorption validation gate, user-provided Discord setup and
controlled multi-user testing. Synthetic canary tests are not that live gate.
