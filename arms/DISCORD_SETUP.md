# Foreground Discord pilot

This is a partial pilot: conversation, source-submission and approved-job execution are implemented;
automatic shared starter-thread creation is available, while complete recovery
controls and reactions remain pending. Publication commands are mock-tested, not
live-validated. Do not treat it as the completed team-sharing release.

## After an interrupted backend process

Stop the Gateway and verify any orphaned Pi/worker processes have stopped before
running `python -m research_arms.recovery --root /absolute/arms-runtime
--spaces-root /absolute/brain-spaces --confirm-workers-stopped` from the backend
environment. The command refuses an active supervisor lock, makes a private
verified backup, and quarantines interrupted work without replaying anything.
It does not recover or rerun paid Brain jobs. Inspect those ledgers separately.
Restart the Gateway only after reviewing the recovery report. Existing remote
paper threads may then be reconciled with `/paper reconcile`; absent resources
and partial forks still require inspection. Never infer that a call failed merely
because the process or connection disappeared.

## Create a dedicated application

Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
Use a dedicated bot because command synchronization replaces its global command
set. Keep the bot token only in the backend's `DISCORD_BOT_TOKEN` environment
variable. Never paste it into a conversation, commit, command argument or Pi tool.

The pilot uses Guilds, Guild Messages and Direct Messages Gateway intents. Leave
privileged Message Content, Server Members and Presence intents off for now.
Without Message Content, use DMs, explicit bot mentions or slash commands. A
reply whose content Discord omits receives a generic prompt to use `/ask`.
Reactions will need their own explicitly tested intent setup.

For OAuth2 installation, select `bot` and `applications.commands`. Grant View
Channels, Send Messages, Send Messages in Threads, Read Message History, and
Attach Files and Create Public Threads in the selected research channel. Thread
creation follows successful shared source preparation. Do not grant Administrator.
Message pinning is not implemented.
Once the application ID is available, generate the exact installation URL using
Discord's OAuth2 URL Generator and review the requested permissions there.

## Local configuration

Install the independently packaged components in an external Python environment:

```sh
python -m pip install -e ./brain -e './arms[discord]'
```

The Brain space registry and Arms registry must already exist. Trusted local
administration uses `SpaceRegistry.register`, `ArmsRegistry.register_principal`
and `ArmsRegistry.bind_channel` to associate exact Discord user/channel IDs.
Register personal corpora as personal; create shared projects empty. Only
designated shared channels can be used. Ordinary users cannot register paths,
principals or spaces through the bot.

Old Arms registries require explicit backed-up migrations described in the Arms
README. Existing RUNNING claims require verified process recovery, not a reset.
No startup path migrates or initializes a database silently.

From the repository root, with the environment activated and token configured:

```sh
python -m research_arms.gateway \
  --root /absolute/path/to/arms-runtime \
  --spaces-root /absolute/path/to/brain-space-registry \
  --pi /absolute/path/to/pi \
  --connect --sync-commands
```

Omit `--sync-commands` on subsequent runs. No HTTP port or tunnel is opened.
Use foreground mode; no LaunchAgent is installed. Stop with Ctrl-C. The Mac must
remain awake and online. Interrupted turns require attention rather than paid
replay; uncertain deliveries require reconciliation. Pi uses its configured
login, with built-in tools and implicit resources disabled.

## First interaction

1. In a DM or designated shared channel, run `/new name:pilot`.
2. Inspect `/session` and `/space`. Verify the audience before asking anything.
3. `/ask question:...` queues a Pi turn. An optional UTF-8 `.txt`/`.md` attachment
   may supplement it, up to 20,000 combined characters.
4. The acknowledgment is ephemeral; the eventual answer goes to the same DM or
   shared channel through the durable outbox. Long answers use Markdown files.
5. `/resume conversation_id:...` explicitly switches selection. `/stop` cancels
   queued work and interrupts the active turn; interrupted context cannot silently
   resume. Normal idle conversations remain usable.
   `/fork answer_message_id:... name:...` branches at that exact completed bot
   answer in this channel/DM and selects the new conversation. Enable Discord
   Developer Mode to copy the answer's message ID. This does not copy later turns
   or broaden access. The original conversation remains available. Failed partial
   forks require local reconciliation rather than automatic retry. Branching
   requires local Node and the Pi SDK; trusted backend flags `--fork-node` and
   `--fork-sdk` can override their detected paths.
6. A DM or explicit bot mention continues the selected conversation. Replying
   to a recorded bot answer selects its mapped conversation and supplies the
   exact stored answer as a bounded quote. Unknown reply anchors do not fall back
   to the active session. Unrelated channel chatter is not retained. Replies to
   a recorded paper starter route into that paper's thread.
7. `/paper status`, `/paper brief`, `/paper cards` and `/paper evidence` read
   exact document/block IDs from the current space. They do not spend tokens.
   Brief is currently a metadata overview, not a generated paper summary.
8. Owners/maintainers can inspect an existing scoped absorption plan with
   `/paper job job_id:...`. `/paper approve job_id:... plan_digest:... confirm:true`
   explicitly approves that exact plan for later paid execution. It does not
   launch providers from the command handler. Enable the background paid worker
   only by adding `--run-approved-absorption` to the foreground startup command;
   unscoped legacy
   plans cannot be approved through this command.
9. `/paper add url:https://arxiv.org/abs/...` queues source preparation. It records
   explicit default ceilings of 32 calls and 2,000,000 reserved tokens, overridable
   by the command options; it does not approve spending. Inspect the returned ID
   with `/paper submission submission_id:...`, then inspect its job before approval.
   Failed/interrupted source requests require local reconciliation. No automatic
   retry is performed. In a configured shared channel, source completion queues
   its starter and dedicated thread automatically; `/paper submission` reports
   that separate job's status. DMs do not create shared threads. For an existing
   source job, `/paper thread job_id:...` is also available. Repeating it reuses the
   same revision's thread. Different revisions get different threads. Uncertain
   sends can be checked with `/paper reconcile job_id:... starter_message_id:...`.
   This only adopts verified existing Discord resources; it never resends. Missing
   messages, threads or verification fields remain unresolved. In-flight states
   after a killed process and conflicting sessions still need local inspection;
   do not manually repeat Discord writes.

Paid execution is off by default. With `--run-approved-absorption`, the worker
considers only Gateway submission jobs that already have an explicit approval.

For human scientific review, run `/card show object_id:...` and inspect the card
and evidence. Use its `updated_at` value as `/card review`'s `expected_version`.
Choose ACCEPTED, DISPUTED or REJECTED; dispute/reject require a note. Acceptance
means faithful representation of the cited source and qualifications, and enables
reliable recall eligibility—not universal correctness. Shared maintainers and
personal owners can submit decisions. Reload after a conflict; duplicate stale
submissions do not add a second event. Large cards must be reviewed in the local
workbench. This workflow does not let Pi review cards.

The paid worker rechecks the source submitter and approver's maintainer/Discord access before
provider dispatch, targets that exact job, and preserves existing ceilings and
attempt ledgers. Set `OPENAI_API_KEY` only in the backend environment. Shutdown
blocks future dispatches and waits for the currently dispatched call to settle;
it cannot undo an in-flight provider request. Failures require attention, not
automatic paid replay. This path is mock-tested, not live-validated.

Do not submit sensitive private information in shared command arguments. Shared
means the configured channel audience, not just the person invoking a command.

## Explicit publication workflow

1. In your DM, use `/publish prepare destination_channel_id:... note_ids:...`
   and/or `paper_block_ids:...`. Use whitespace-separated IDs; the selected paper
   blocks identify pinned source revisions. Your destination channel access is
   checked before preparation. No source paths or session files are accepted.
2. Read the entire preview, including its exact note text, evidence dependencies,
   source revision/hash/license, and shared audience. Large previews arrive as JSON.
   Use `/publish show` to reopen it privately.
3. In the DM, `/publish consent publication_id:... digest:... confirm:true` gives
   explicit source-owner consent. Until then, the preview cannot be displayed in
   a shared channel, even by its owner.
4. A destination maintainer uses `/publish show` in that project channel, then
   `/publish approve publication_id:... digest:... confirm:true`.
5. In the same project, `/publish run publication_id:... digest:... confirm:true`
   executes the approved local transfer. It spends no model tokens and does not
   accept the resulting notes scientifically. Check `/publish show` after a lost
   response. Use `retry:true` only after inspecting an uncertain execution.
6. The owner can `/publish cancel` in the DM before execution. Cancellation does
   not delete already published records or undo a running transfer.

## Verification status

Use `/discussed question:...` to search exchanges indexed in the current space.
Only confirmed bot-directed answers are projected, without provider calls. Results
identify the question author and assistant separately and link to their messages.
Private DM discussions are not searched from shared channels, even for the owner.
Missing results do not prove a topic was never discussed: historical backfill and
Discord edit/deletion integration remain pending. Brain schema 003 and Arms schema
8 require explicit backed-up migration on existing databases. No migration is run
automatically by the Gateway.

Command registration and handlers have offline tests using the installed
discord.py SDK. REST permissions/delivery use synthetic responses. The Gateway
has not been connected to a live Discord application, and live Pi synthesis,
sleep/reconnect behavior and controlled second-user isolation remain open gates.
