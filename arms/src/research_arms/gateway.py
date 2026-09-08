"""Explicit foreground Discord Gateway entry point; no HTTP listener or daemon."""
import argparse
import asyncio
import json
import hashlib
import io
import os

import discord
from discord import app_commands

from research_brain.spaces import SpaceRegistry
from .registry import ArmsRegistry, Unavailable
from .worker import Supervisor
from .discord_access import DiscordAccess
from .discord_io import DiscordSender, assemble_question, download_chunks
from .papers import read_paper
from .absorption import ScopedAbsorption


class ResearchGateway(discord.Client):
    def __init__(self, registry, pi_executable, *, sync_commands=False, rest_client=None,
                 absorption_factory=ScopedAbsorption):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        # Slash commands work without privileged message-content/member intents.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.registry, self.pi_executable = registry, pi_executable
        self.sync_commands = sync_commands
        self.absorption_factory = absorption_factory
        self.rest = rest_client
        self.access = self.supervisor = self.pump = None
        self.running_turns = set()
        self.gateway_online = False
        self.tree = app_commands.CommandTree(self)
        self._commands()

    def _commands(self):
        paper = app_commands.Group(name="paper", description="Read paper records in this destination's Brain space")
        def paper_command(operation):
            async def callback(interaction: discord.Interaction, identifier: str):
                await self.execute(interaction, "paper_" + operation, identifier=identifier)
            paper.command(name=operation, description="Read " + operation + " using an exact document or evidence ID")(callback)
        for operation in ("status", "brief", "cards", "evidence"):
            paper_command(operation)
        @paper.command(name="job", description="Inspect an absorption job and its exact paid-work plan digest")
        async def paper_job(interaction: discord.Interaction, job_id: str):
            await self.execute(interaction, "paper_job", job_id=job_id)

        @paper.command(name="approve", description="Authorize the exact absorption plan for later paid execution")
        async def paper_approve(interaction: discord.Interaction, job_id: str, plan_digest: str, confirm: bool = False):
            await self.execute(interaction, "paper_approve", job_id=job_id, plan_digest=plan_digest, confirm=confirm)
        self.tree.add_command(paper)
        @self.tree.command(name="new", description="Create and select a named research conversation")
        async def new(interaction: discord.Interaction, name: str):
            await self.execute(interaction, "new", name=name)

        @self.tree.command(name="resume", description="Select an exact existing research conversation ID")
        async def resume(interaction: discord.Interaction, conversation_id: str):
            await self.execute(interaction, "resume", conversation_id=conversation_id)

        @self.tree.command(name="session", description="Show the explicitly selected research conversation")
        async def session(interaction: discord.Interaction):
            await self.execute(interaction, "session")

        @self.tree.command(name="space", description="Show this destination's Brain audience and read spaces")
        async def space(interaction: discord.Interaction):
            await self.execute(interaction, "space")

        @self.tree.command(name="ask", description="Queue a question in the selected research conversation")
        async def ask(interaction: discord.Interaction, question: str = "", attachment: discord.Attachment | None = None):
            await self.execute(interaction, "ask", question=question, attachment=attachment)

        @self.tree.command(name="stop", description="Cancel queued work and stop the selected research conversation")
        async def stop(interaction: discord.Interaction):
            await self.execute(interaction, "stop")

    async def setup_hook(self):
        if self.sync_commands:
            await self.tree.sync()

    async def on_ready(self):
        self.gateway_online = True
        if self.supervisor is None:
            self.rest = self.rest or DiscordSender.environment_client()
            self.access = DiscordAccess(self.registry, self.rest, str(self.user.id))
            self.supervisor = Supervisor(self.registry, self.pi_executable,
                                         authorize_turn=self.access.authorize_turn)
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._pump())

    async def on_disconnect(self):
        self.gateway_online = False

    async def on_resumed(self):
        self.gateway_online = True

    async def on_message(self, message):
        if self.access is None or self.user is None or message.author.bot or message.webhook_id:
            return
        actor, channel = str(message.author.id), str(message.channel.id)
        guild = str(message.guild.id) if message.guild else None
        reference = str(message.reference.message_id) if message.reference and message.reference.message_id else None
        mentioned = any(user.id == self.user.id for user in message.mentions)
        with self.registry.connect(readonly=True) as db:
            known_reply = reference is not None and db.execute(
                "SELECT 1 FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.discord_message_id=? AND o.state='DELIVERED' AND c.channel_id=? AND c.guild_id IS ?",
                (reference, channel, guild)).fetchone() is not None
        if guild is not None and not mentioned and not known_reply:
            return  # Never retain casual channel chatter.
        try:
            destination = await self.access.authorize(actor, channel_id=channel, guild_id=guild)
            if reference is not None and not known_reply:
                raise Unavailable()
            selected = self.registry.resolve_conversation(actor, channel_id=channel, guild_id=guild,
                reply_message_id=reference if known_reply else None)
            text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
            attachments = [{"filename": item.filename, "url": item.url, "size": item.size} for item in message.attachments]
            question = await assemble_question(text, attachments, download_chunks)
            await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
            with self.registry.connect(readonly=True) as db:
                previous = db.execute("SELECT t.* FROM turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.discord_message_id=? AND c.channel_id=? AND c.guild_id IS ?", (str(message.id), channel, guild)).fetchall()
                if previous:
                    if len(previous) != 1: raise Unavailable()
                    old = previous[0]
                    _, scope = self.registry._authorized(db, old["conversation_id"], actor, channel, guild)
                    if old["author"] != scope.principal or old["prompt"] != question:
                        raise Unavailable()
                    return
            self.registry.enqueue(selected["conversation_id"], actor, channel_id=channel, guild_id=guild,
                message_id=str(message.id), prompt=question, anchor_turn_id=selected["anchor_turn_id"])
        except Exception:
            # Only send generic feedback after a fresh destination check. No
            # private IDs/titles, raw errors or provider details are included.
            try:
                await self.access.authorize(actor, channel_id=channel, guild_id=guild)
                nonce = str(int(hashlib.sha256((str(message.id) + ":notice").encode()).hexdigest()[:16], 16))
                await self.rest.post(f"channels/{channel}/messages", json={
                    "content": "Question unavailable. Select a conversation with /new or /resume, then use /ask or mention the bot. Questions must be at most 20,000 characters with UTF-8 .txt/.md attachments.",
                    "allowed_mentions": {"parse": [], "replied_user": False}, "flags": 4100,
                    "nonce": nonce, "enforce_nonce": True})
            except Exception:
                pass  # No retry of an uncertain status notice.

    async def execute(self, interaction, command, **options):
        # Acknowledge within Discord's deadline before fresh REST authorization.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.access is None or self.supervisor is None or interaction.user.bot:
                raise Unavailable()
            actor, channel = str(interaction.user.id), str(interaction.channel_id)
            guild = str(interaction.guild_id) if interaction.guild_id is not None else None
            destination = await self.access.authorize(actor, channel_id=channel, guild_id=guild)
            result = await self.handle(command, actor, channel, guild, str(interaction.id), destination, **options)
            # Do not return selected private metadata following a permission change.
            await self.access.authorize(actor, channel_id=channel, guild_id=guild,
                                        expected_space=destination["space_id"])
            text = json.dumps(result, ensure_ascii=False)
            if len(text) > 1700:
                if len(text.encode()) > 16000: raise ValueError("Result exceeds bound")
                await interaction.edit_original_response(content="Research result attached; inspect provenance and review labels.",
                    attachments=[discord.File(io.BytesIO(text.encode()), filename="research-result.json")],
                    allowed_mentions=discord.AllowedMentions.none())
                return
        except ValueError:
            text = ("Approval status not confirmed. Inspect /paper job and use its exact plan digest with confirm:true."
                    if command == "paper_approve" else
                    "Invalid or oversized request. Use an exact conversation ID and at most 20,000 characters of UTF-8 text.")
        except Exception:
            text = "Research operation unavailable. Check your selected session, access, or backend recovery status."
        await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())

    async def handle(self, command, actor, channel, guild, message_id, destination, **options):
        """Internal authenticated handler; never expose caller-supplied destination data."""
        if command in {"paper_job", "paper_approve"}:
            def operate():
                service = self.absorption_factory(self.registry, actor, destination["space_id"])
                if command == "paper_job":
                    return service.preview(options["job_id"])
                if options.get("confirm") is not True:
                    raise ValueError("Explicit paid-work confirmation required")
                return service.approve(options["job_id"], options["plan_digest"], live=True)
            return await asyncio.to_thread(operate)
        if command.startswith("paper_"):
            return await asyncio.to_thread(read_paper, self.registry, actor, destination,
                                           command.removeprefix("paper_"), options["identifier"])
        if command == "new":
            ident = self.registry.new_conversation(actor, channel_id=channel, guild_id=guild,
                parent_channel_id=destination["parent_channel_id"], name=options["name"], request_id=message_id)
            self.registry.select_conversation(ident, actor, channel_id=channel, guild_id=guild)
        elif command == "resume":
            self.registry.select_conversation(options["conversation_id"], actor, channel_id=channel, guild_id=guild)
        elif command not in {"space", "session", "ask", "stop"}:
            raise Unavailable()
        if command == "space":
            try:
                return self.registry.resolve_conversation(actor, channel_id=channel, guild_id=guild)
            except Unavailable:
                return {**destination, "read_spaces": [destination["space_id"]], "selected_conversation": None}
        selected = self.registry.resolve_conversation(actor, channel_id=channel, guild_id=guild)
        if command == "ask":
            attachment = options.get("attachment")
            attachments = [{"filename": attachment.filename, "url": attachment.url, "size": attachment.size}] if attachment else []
            question = await assemble_question(options.get("question", ""), attachments, download_chunks)
            await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
            # A replayed interaction retains its original conversation even if
            # the active selection changed after the first enqueue.
            with self.registry.connect(readonly=True) as db:
                previous = db.execute("SELECT t.* FROM turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.discord_message_id=? AND c.channel_id=? AND c.guild_id IS ?", (message_id, channel, guild)).fetchall()
                if previous:
                    if len(previous) != 1: raise Unavailable()
                    old = previous[0]
                    _, old_scope = self.registry._authorized(db, old["conversation_id"], actor, channel, guild)
                    if old["author"] != old_scope.principal: raise Unavailable()
                    if old["prompt"] != question: raise ValueError("Duplicate question changed")
                    return {"queued_turn": old["id"], "conversation_id": old["conversation_id"]}
            ident = self.registry.enqueue(selected["conversation_id"], actor, channel_id=channel,
                guild_id=guild, message_id=message_id, prompt=question)
            return {"queued_turn": ident, **selected}
        if command == "stop":
            return await self.supervisor.stop(selected["conversation_id"], actor, channel_id=channel, guild_id=guild)
        return selected

    async def _deliver(self, turn_id):
        async def authorize(guild, channel):
            destination = await self.access.authorize_turn(turn_id)
            return destination["guild_id"] == guild and destination["channel_id"] == channel
        sender = DiscordSender(self.registry, self.rest, str(self.user.id), authorize)
        await sender.deliver(turn_id)

    async def _pump(self):
        while not self.is_closed():
            await self.wait_until_ready()
            if not self.gateway_online:
                await asyncio.sleep(2)
                continue
            await self.supervisor.maintain()
            for task in list(self.running_turns):
                if task.done():
                    self.running_turns.remove(task)
                    try: task.result()
                    except Exception: pass  # Registry already quarantines uncertain turns.
            with self.registry.connect(readonly=True) as db:
                queued = db.execute("SELECT COUNT(*) FROM turns WHERE status='QUEUED'").fetchone()[0]
                deliveries = [r[0] for r in db.execute("SELECT turn_id FROM outbox WHERE state='PENDING' ORDER BY created_at,id LIMIT 10")]
            for _ in range(min(queued, 2 - len(self.running_turns))):
                self.running_turns.add(asyncio.create_task(self.supervisor.run_once()))
            for ident in deliveries:
                try: await self._deliver(ident)
                except Exception: pass  # UNKNOWN/revoked records are never automatically resent.
            await asyncio.sleep(2)

    async def close(self):
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        for task in self.running_turns: task.cancel()
        await asyncio.gather(*self.running_turns, return_exceptions=True)
        if self.supervisor: await self.supervisor.close()
        if self.rest: await self.rest.aclose()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description="Foreground Arms Discord Gateway")
    parser.add_argument("--root", required=True, help="Existing Arms operational directory")
    parser.add_argument("--spaces-root", required=True, help="Existing Brain space registry")
    parser.add_argument("--pi", required=True, help="Absolute Pi executable")
    parser.add_argument("--connect", action="store_true", help="Explicitly connect to Discord")
    parser.add_argument("--sync-commands", action="store_true", help="Replace this bot application's global commands")
    args = parser.parse_args()
    if not args.connect: parser.error("--connect is required; no connection made")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token: parser.error("DISCORD_BOT_TOKEN is required in the backend environment")
    registry = ArmsRegistry(args.root, SpaceRegistry(args.spaces_root))
    client = ResearchGateway(registry, args.pi, sync_commands=args.sync_commands)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
