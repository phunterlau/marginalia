# Foreground Discord pilot

This is a partial pilot: conversation slash commands are implemented; paper
commands, reactions and publication controls
are still pending. Do not treat it as the completed team-sharing release.

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
Attach Files in the selected research channel. Do not grant Administrator.
Paper-thread creation/pinning permissions are deferred until those commands ship.
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
6. A DM or explicit bot mention continues the selected conversation. Replying
   to a recorded bot answer selects its mapped conversation and supplies the
   exact stored answer as a bounded quote. Unknown reply anchors do not fall back
   to the active session. Unrelated channel chatter is not retained. Paper-starter
   reply routing is still pending.

Do not submit sensitive private information in shared command arguments. Shared
means the configured channel audience, not just the person invoking a command.

## Verification status

Command registration and handlers have offline tests using the installed
discord.py SDK. REST permissions/delivery use synthetic responses. The Gateway
has not been connected to a live Discord application, and live Pi synthesis,
sleep/reconnect behavior and controlled second-user isolation remain open gates.
