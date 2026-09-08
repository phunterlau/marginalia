"""Explicit foreground Discord Gateway entry point; no HTTP listener or daemon."""
import argparse
import asyncio
import json
import hashlib
import io
import os
import logging
import traceback
from pathlib import Path
import shutil

import discord
from discord import app_commands

from research_brain.spaces import SpaceRegistry
from .registry import ArmsRegistry, Unavailable
from .worker import Supervisor
from .discord_access import DiscordAccess
from .discord_io import DiscordSender, assemble_question, download_chunks, EmptyQuestion, InvalidQuestionText
from .papers import read_paper
from .absorption import ScopedAbsorption
from .submissions import PaperSubmissions
from .paid_worker import PaidAbsorptionWorker
from .paper_threads import PaperThreads
from .thread_worker import PaperThreadWorker
from .reviews import CardReviews
from .publication_commands import handle as handle_publication
from .discussion_worker import DiscussionWorker
from .discussion_reconcile import DiscussionReconciler
from .research_commands import recall as research_recall
from .research_commands import compare as research_compare
from .research_commands import frontier as research_frontier
from .saves import save_excerpt
from .deep_dives import preview as preview_deep_dive, run as run_deep_dive
from .feedback import observe as observe_feedback, view as feedback_view, clear as clear_feedback
from .feedback_reconcile import reconcile as reconcile_feedback, FeedbackReconciler
from .discussion_edits import observe as observe_discussion_edit
from .forks import fork_conversation, recover_fork
from .registry import snowflake
from research_brain.jobs import SpendingLimits


def report_command_error(command, exc):
    """Diagnostic code locations only: no exception text, locals, or user input."""
    frames = traceback.extract_tb(exc.__traceback__)
    logging.getLogger(__name__).warning(json.dumps({"event": "command_failed",
        "command": command, "error_type": type(exc).__name__,
        "locations": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in frames[-6:]]}))


class ResearchGateway(discord.Client):
    def __init__(self, registry, pi_executable, *, sync_commands=False, rest_client=None,
                 absorption_factory=ScopedAbsorption, run_approved_absorption=False,
                 fork_node=None, fork_sdk=None, fork_handler=fork_conversation):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.guild_reactions = True
        intents.dm_reactions = True
        # Slash commands work without privileged message-content/member intents.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.registry, self.pi_executable = registry, pi_executable
        self.sync_commands = sync_commands
        self.absorption_factory = absorption_factory
        self.run_approved_absorption = run_approved_absorption
        self.fork_node = fork_node or shutil.which("node")
        self.fork_sdk = fork_sdk or str(Path(pi_executable).resolve().parent / "index.js")
        self.fork_handler = fork_handler
        self.paid_worker = self.paid_task = None
        self.rest = rest_client
        self.access = self.supervisor = self.pump = None
        self.running_turns = set()
        self.gateway_online = False
        self.submissions = self.source_task = None
        self.thread_worker = self.thread_task = None
        self.publication_tasks = set()
        self.publication_stopping = False
        self.discussion_worker = self.discussion_task = None
        self.reconciler = DiscussionReconciler(self)
        self.reconcile_task = None
        self.feedback_lock = asyncio.Lock()
        self.feedback_reconciler = FeedbackReconciler(self)
        self.feedback_task = None
        self.tree = app_commands.CommandTree(self)
        self._commands()

    def _commands(self):
        @self.tree.command(name="delivery-recover", description="Confirm an existing answer to your turn; never resend")
        async def delivery_recover(interaction: discord.Interaction, turn_id: str, answer_message_id: str):
            await self.execute(interaction, "delivery_recover", turn_id=turn_id, answer_message_id=answer_message_id)
        @self.tree.command(name="fork-recover", description="Adopt an exact verified partial fork; never recreate or rewrite sessions")
        async def fork_recover(interaction: discord.Interaction, request_id: str):
            await self.execute(interaction, "fork_recover", request_id=request_id)
        @self.tree.command(name="deep-dive", description="Preview or approve a pending deep dive; reactions alone never run it")
        async def deep_dive(interaction: discord.Interaction, message_id: str, confirm: bool = False, digest: str | None = None):
            await self.execute(interaction, "deep_dive", message_id=message_id, confirm=confirm, digest=digest)
        @self.tree.command(name="interests", description="Show project interest or your personal reaction signals; no model work")
        async def interests(interaction: discord.Interaction, spaces: str = "", refresh_message: str | None = None):
            await self.execute(interaction, "interests", spaces=spaces, refresh_message=refresh_message)
        @self.tree.command(name="frontier", description="Inspect a Brain research thread, including unreviewed ideas and proposed tests")
        async def frontier(interaction: discord.Interaction, thread_id: str, after_id: str = ""):
            await self.execute(interaction, "frontier", thread_id=thread_id, after_id=after_id)
        @self.tree.command(name="save", description="Preview or explicitly save an exact answer excerpt as unreviewed memory")
        async def save(interaction: discord.Interaction, answer_message_id: str, revision: int, start: int, end: int,
                       confirm: bool = False, digest: str | None = None):
            await self.execute(interaction, "save", answer_message_id=answer_message_id, revision=revision,
                start=start, end=end, confirm=confirm, digest=digest)
        @self.tree.command(name="brainstorm", description="Start a separate blind-first research conversation; follow up to use memory")
        async def brainstorm(interaction: discord.Interaction, question: str, paper: str | None = None):
            await self.execute(interaction, "brainstorm", question=question, paper=paper)
        @self.tree.command(name="compare", description="Compare reliable evidence for 2–4 exact document@vN paper revisions")
        async def compare(interaction: discord.Interaction, papers: str, question: str):
            await self.execute(interaction, "compare", papers=papers, question=question)
        @self.tree.command(name="recall", description="Find source evidence and accepted research records in this space")
        async def recall(interaction: discord.Interaction, question: str):
            await self.execute(interaction, "recall", question=question)
        @self.tree.command(name="discussed", description="Search recorded bot-directed exchanges in this Brain space")
        async def discussed(interaction: discord.Interaction, question: str):
            await self.execute(interaction, "discussed", question=question)
        publish = app_commands.Group(name="publish", description="Explicit private-to-shared publication with owner consent")
        @publish.command(name="prepare", description="DM only: preview selected notes/papers for a shared destination channel")
        async def publish_prepare(interaction: discord.Interaction, destination_channel_id: str, note_ids: str = "", paper_block_ids: str = ""):
            await self.execute(interaction, "publish_prepare", destination_channel_id=destination_channel_id,
                note_ids=note_ids, paper_block_ids=paper_block_ids)
        @publish.command(name="show", description="Inspect an authorized publication preview or status")
        async def publish_show(interaction: discord.Interaction, publication_id: str):
            await self.execute(interaction, "publish_show", publication_id=publication_id)
        def decision_command(action):
            async def callback(interaction: discord.Interaction, publication_id: str, digest: str, confirm: bool = False):
                await self.execute(interaction, "publish_" + action, publication_id=publication_id, digest=digest, confirm=confirm)
            publish.command(name=action, description="Explicit publication " + action + "; requires the exact preview digest")(callback)
        for action in ("consent", "approve", "cancel"): decision_command(action)
        @publish.command(name="run", description="Shared maintainer: execute an approved publication; no model calls")
        async def publish_run(interaction: discord.Interaction, publication_id: str, digest: str, confirm: bool = False, retry: bool = False):
            await self.execute(interaction, "publish_run", publication_id=publication_id, digest=digest, confirm=confirm, retry=retry)
        self.tree.add_command(publish)
        card = app_commands.Group(name="card", description="Inspect evidence-backed research cards and record human review")
        @card.command(name="show", description="Read a card, its evidence and the version required for review")
        async def card_show(interaction: discord.Interaction, object_id: str):
            await self.execute(interaction, "card_show", object_id=object_id)

        @card.command(name="review", description="Human source-faithfulness review; acceptance enables reliable recall")
        @app_commands.choices(decision=[app_commands.Choice(name=value, value=value) for value in ("ACCEPTED", "DISPUTED", "REJECTED")])
        async def card_review(interaction: discord.Interaction, object_id: str, decision: str, expected_version: str, note: str = ""):
            await self.execute(interaction, "card_review", object_id=object_id, decision=decision,
                expected_version=expected_version, note=note)
        self.tree.add_command(card)
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

        @paper.command(name="add", description="Queue arXiv source ingestion and prepare a paid-work plan without running it")
        async def paper_add(interaction: discord.Interaction, url: str, max_calls: int = 32, max_reserved_tokens: int = 2000000):
            await self.execute(interaction, "paper_add", url=url, max_calls=max_calls, max_reserved_tokens=max_reserved_tokens)

        @paper.command(name="submission", description="Inspect the durable source-preparation request")
        async def paper_submission(interaction: discord.Interaction, submission_id: str):
            await self.execute(interaction, "paper_submission", submission_id=submission_id)

        @paper.command(name="retry", description="Explicitly retry your failed source preparation; no paid approval")
        async def paper_retry(interaction: discord.Interaction, submission_id: str):
            await self.execute(interaction, "paper_retry", submission_id=submission_id)

        @paper.command(name="thread", description="Create or reuse the pinned shared-paper starter and thread")
        async def paper_thread(interaction: discord.Interaction, job_id: str):
            await self.execute(interaction, "paper_thread", job_id=job_id)

        @paper.command(name="reconcile", description="Recover a paper thread from verified existing Discord resources")
        async def paper_reconcile(interaction: discord.Interaction, job_id: str, starter_message_id: str):
            await self.execute(interaction, "paper_reconcile", job_id=job_id, starter_message_id=starter_message_id)

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

        @self.tree.command(name="fork", description="Branch from an exact completed bot answer in this channel")
        async def fork(interaction: discord.Interaction, answer_message_id: str, name: str = "Research fork"):
            await self.execute(interaction, "fork", answer_message_id=answer_message_id, name=name)

        @self.tree.command(name="session", description="Show the explicitly selected research conversation")
        async def session(interaction: discord.Interaction):
            await self.execute(interaction, "session")

        @self.tree.command(name="space", description="Show this destination's Brain audience and read spaces")
        async def space(interaction: discord.Interaction):
            await self.execute(interaction, "space")

        @self.tree.command(name="ask", description="Queue a question in the selected research conversation")
        async def ask(interaction: discord.Interaction, question: str, attachment: discord.Attachment | None = None):
            await self.execute(interaction, "ask", question=question, attachment=attachment)

        @self.tree.command(name="stop", description="Cancel queued work and stop the selected research conversation")
        async def stop(interaction: discord.Interaction):
            await self.execute(interaction, "stop")

    async def setup_hook(self):
        if self.sync_commands:
            await self.tree.sync()

    async def on_ready(self):
        self.gateway_online = True
        self.reconciler.restart()
        self.feedback_reconciler.restart()
        if self.supervisor is None:
            self.rest = self.rest or DiscordSender.environment_client()
            self.access = DiscordAccess(self.registry, self.rest, str(self.user.id))
            self.supervisor = Supervisor(self.registry, self.pi_executable,
                                         authorize_turn=self.access.authorize_turn)
            self.submissions = PaperSubmissions(self.registry, self.access, service_factory=self.absorption_factory,
                automatic_threads=True)
            self.thread_worker = PaperThreadWorker(self.registry, self.access, self.submissions,
                PaperThreads(self.registry, self.access, self.rest, str(self.user.id), service_factory=self.absorption_factory))
            self.paid_worker = PaidAbsorptionWorker(self.registry, self.access,
                enabled=self.run_approved_absorption, service_factory=self.absorption_factory)
            self.discussion_worker = DiscussionWorker(self.registry, self.access)
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._pump())

    async def on_disconnect(self):
        self.gateway_online = False

    async def on_resumed(self):
        self.gateway_online = True
        self.reconciler.restart()
        self.feedback_reconciler.restart()

    async def _feedback(self, payload, active):
        if self.access is None: return
        async with self.feedback_lock:
            try: await observe_feedback(self, payload, active=active)
            except Exception:
                # No channel feedback or private metadata on failed authorization.
                pass

    async def on_raw_reaction_add(self, payload):
        await self._feedback(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self._feedback(payload, False)

    async def on_raw_reaction_clear(self, payload):
        async with self.feedback_lock:
            clear_feedback(self.registry, guild=str(payload.guild_id) if payload.guild_id else None,
                channel=str(payload.channel_id), message=str(payload.message_id))

    async def on_raw_reaction_clear_emoji(self, payload):
        if payload.emoji.id is not None: return
        async with self.feedback_lock:
            clear_feedback(self.registry, guild=str(payload.guild_id) if payload.guild_id else None,
                channel=str(payload.channel_id), message=str(payload.message_id), emoji=str(payload.emoji))

    async def on_raw_message_delete(self, payload):
        async with self.feedback_lock:
            guild = str(payload.guild_id) if payload.guild_id else None
            channel, message = str(payload.channel_id), str(payload.message_id)
            clear_feedback(self.registry, guild=guild, channel=channel, message=message)
            self.registry.discussion_message_deleted(guild_id=guild, channel_id=channel, message_id=message)

    async def on_raw_message_edit(self, payload):
        if self.access is None or self.rest is None or self.user is None or not payload.data.get("edited_timestamp"):
            return  # Embed-only updates are not content edits.
        try:
            await observe_discussion_edit(self, guild=str(payload.guild_id) if payload.guild_id else None,
                channel=str(payload.channel_id), message=str(payload.message_id), edited_at=payload.data["edited_timestamp"])
        except Exception:
            pass  # Known edits remain explicitly unavailable until reconciliation.

    async def on_raw_bulk_message_delete(self, payload):
        for ident in payload.message_ids:
            from types import SimpleNamespace
            await self.on_raw_message_delete(SimpleNamespace(guild_id=payload.guild_id,
                channel_id=payload.channel_id, message_id=ident))

    async def on_message(self, message):
        if self.access is None or self.user is None or message.author.bot or message.webhook_id:
            return
        actor, channel = str(message.author.id), str(message.channel.id)
        guild = str(message.guild.id) if message.guild else None
        reference = str(message.reference.message_id) if message.reference and message.reference.message_id else None
        mentioned = any(user.id == self.user.id for user in message.mentions)
        with self.registry.connect(readonly=True) as db:
            paper_reply = db.execute("SELECT * FROM paper_threads WHERE starter_id=? AND parent_id=? AND guild_id IS ? AND state='COMPLETE'", (reference, channel, guild)).fetchone() if reference else None
            known_reply = reference is not None and db.execute(
                "SELECT 1 FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.discord_message_id=? AND o.state='DELIVERED' AND c.channel_id=? AND c.guild_id IS ?",
                (reference, channel, guild)).fetchone() is not None
        if guild is not None and not mentioned and not known_reply and paper_reply is None:
            return  # Never retain casual channel chatter.
        try:
            destination = await self.access.authorize(actor, channel_id=channel, guild_id=guild)
            if paper_reply is not None:
                await self.access.authorize(actor, channel_id=paper_reply["thread_id"], guild_id=guild, expected_space=destination["space_id"])
                channel = paper_reply["thread_id"]
            if reference is not None and not known_reply and paper_reply is None:
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
                message_id=str(message.id), prompt=question, anchor_turn_id=selected["anchor_turn_id"],
                question_channel_id=str(message.channel.id))
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
            if command == "publish_show" and guild is not None:
                # Owner cancellation while a preview was being loaded must
                # prevent its later delivery to the shared audience.
                result = await handle_publication(self, command, actor, channel, guild,
                    str(interaction.id), destination, **options)
            text = json.dumps(result, ensure_ascii=False)
            if len(text) > 1700:
                if len(text.encode()) > (110000 if command.startswith("publish_") else 66000 if command in {"discussed", "recall", "compare", "save", "frontier", "deep_dive"} else 16000): raise ValueError("Result exceeds bound")
                await interaction.edit_original_response(content="Research result attached; inspect provenance and review labels.",
                    attachments=[discord.File(io.BytesIO(text.encode()), filename="research-result.json")],
                    allowed_mentions=discord.AllowedMentions.none())
                return
        except EmptyQuestion as exc:
            report_command_error(command, exc)
            text = "No question text was received. Select /ask, fill its required question field, then send. For an attachment, enter a short instruction such as 'Answer the attached question'."
        except InvalidQuestionText as exc:
            report_command_error(command, exc)
            text = "The question contains a NUL control character. Paste it as plain text or use a UTF-8 .txt/.md attachment without NUL characters."
        except ValueError as exc:
            report_command_error(command, exc)
            text = ("Deep dive not confirmed. Preview an active 🔬 request in this channel, then repeat with confirm:true and the exact digest. Owner/maintainer approval is required; stale or oversized source answers cannot be queued."
                    if command == "deep_dive" else
                    "Frontier unavailable or oversized. Use an exact Brain research-thread obj_ID and its returned next_cursor as after_id. Oversized records require the local reader."
                    if command == "frontier" else
                    "Save not confirmed. Preview a current /discussed revision with 0-based start/end character offsets (end exclusive, at most 8,000 characters), then repeat with confirm:true and its exact digest. Shared saves require a maintainer."
                    if command == "save" else
                    "Approval status not confirmed. Inspect /paper job and use its exact plan digest with confirm:true."
                    if command == "paper_approve" else
                    "Review not confirmed. Reload /card show for the current version; dispute/reject require a note. Oversized cards need the local review workbench."
                    if command.startswith("card_") else
                    "Publication not confirmed. Inspect /publish show, use its exact digest and confirm:true. Prepare/consent/cancel belong in DMs; approve/run belong in the destination channel. Uncertain runs require inspection before retry:true."
                    if command.startswith("publish_") else
                    "Comparison unavailable or oversized. Use 2–4 distinct doc_ID@vN selections in this space and a question of at most 2,000 characters. Narrow the query if necessary."
                    if command == "compare" else
                    "Invalid or oversized request. Use an exact conversation ID and at most 20,000 characters of UTF-8 text.")
        except Exception as exc:
            report_command_error(command, exc)
            text = "Research operation unavailable. Check your selected session, access, or backend recovery status."
        if command == "fork":
            text += f" Fork recovery request ID: {interaction.id}. Use /fork-recover only after any interrupted worker is stopped."
        await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())

    async def handle(self, command, actor, channel, guild, message_id, destination, **options):
        """Internal authenticated handler; never expose caller-supplied destination data."""
        if command == "delivery_recover":
            ident = options["turn_id"]
            if not isinstance(ident, str) or not 1 <= len(ident) <= 100:
                raise Unavailable()
            with self.registry.connect(readonly=True) as db:
                row = db.execute("SELECT t.*,o.id AS delivery_id,o.state AS delivery_state FROM turns t JOIN outbox o ON o.turn_id=t.id WHERE t.id=?", (ident,)).fetchone()
                if row is None or row["delivery_state"] != "UNKNOWN": raise Unavailable()
                _, scope = self.registry._authorized(db, row["conversation_id"], actor, channel, guild,
                    statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
                if row["author"] != scope.principal or scope.writable_space != destination["space_id"]:
                    raise Unavailable()
            async def authorize(remote_guild, remote_channel):
                await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
                return remote_guild == guild and remote_channel == channel
            sender = DiscordSender(self.registry, self.rest, str(self.user.id), authorize)
            recovered = await sender.reconcile(row["delivery_id"], options["answer_message_id"])
            return {"answer_message_id": recovered, "notice": "Existing answer confirmed. No message was resent or model called."}
        if command == "fork_recover":
            async def authorize():
                await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
            conversation = await recover_fork(self.supervisor, request_id=options["request_id"], actor=actor,
                channel_id=channel, guild_id=guild, authorize=authorize)
            return {"conversation_id": conversation, "notice": "Verified existing fork recovered. Use /resume to select it. No model call or session rewrite occurred."}
        if command == "deep_dive":
            async with self.feedback_lock:
                if options.get("confirm", False):
                    return run_deep_dive(self.registry, actor, guild, channel, options["message_id"], digest=options.get("digest"),
                        request_id=message_id, expected_space=destination["space_id"], parent_channel_id=destination.get("parent_channel_id"))
                result = preview_deep_dive(self.registry, actor, guild, channel, options["message_id"])
                if result["space_id"] != destination["space_id"]: raise Unavailable()
                return result
        if command == "interests":
            attached = options.get("spaces", "")
            if not isinstance(attached, str) or len(attached) > 1300 or len(attached.split()) > 16 or (guild is not None and attached.strip()):
                raise ValueError("Shared interest views cannot attach other spaces")
            with self.registry.connect(readonly=True) as db:
                principal = self.registry._principal(db, actor)["principal"]
            scope = self.registry.spaces.scope(principal, conversation_id="feedback-view", writable_space=destination["space_id"], read_spaces=tuple(attached.split()))
            if options.get("refresh_message") is not None:
                async with self.feedback_lock:
                    await reconcile_feedback(self, actor=actor, guild=guild, channel=channel, message=options["refresh_message"])
            return feedback_view(self.registry, scope, shared=guild is not None)
        if command == "frontier":
            return await research_frontier(self.registry, actor, destination, options["thread_id"], after_id=options.get("after_id", ""))
        if command == "save":
            return await asyncio.to_thread(save_excerpt, self.registry, actor, channel, guild,
                options["answer_message_id"], revision=options["revision"], start=options["start"], end=options["end"],
                confirm=options.get("confirm", False), digest=options.get("digest"))
        if command == "brainstorm":
            question = options["question"]
            if not isinstance(question, str) or not question.strip() or len(question) > 20000 or "\x00" in question:
                raise ValueError("Brainstorm question must contain 1..20000 characters")
            conversation = self.registry.new_conversation(actor, channel_id=channel, guild_id=guild,
                parent_channel_id=destination.get("parent_channel_id"), name="Research brainstorm",
                request_id=message_id, blind_first=True, paper_pin=options.get("paper"))
            turn = self.registry.enqueue(conversation, actor, channel_id=channel, guild_id=guild,
                message_id=message_id, prompt=question, question_is_message=False)
            self.registry.select_conversation(conversation, actor, channel_id=channel, guild_id=guild)
            return {"conversation_id": conversation, "queued_turn": turn, "stage": "blind_first",
                "notice": "A separate Pi conversation will produce an unreviewed draft with Brain retrieval disabled. After it completes, reply or use /ask for memory-assisted exploration. Nothing is automatically accepted as research memory."}
        if command == "compare":
            return await research_compare(self.registry, actor, destination, options["papers"], options["question"])
        if command == "recall":
            return await research_recall(self.registry, actor, destination, options["question"])
        if command == "discussed":
            with self.registry.connect(readonly=True) as db:
                principal = self.registry._principal(db, actor)["principal"]
            scope = self.registry.spaces.scope(principal, conversation_id="discussion-search", writable_space=destination["space_id"])
            return await asyncio.to_thread(self.registry.spaces.read, scope, destination["space_id"],
                "search_discussions", options["question"], limit=3)
        if command.startswith("publish_"):
            await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
            return await handle_publication(self, command, actor, channel, guild, message_id, destination, **options)
        if command in {"card_show", "card_review"}:
            await self.access.authorize(actor, channel_id=channel, guild_id=guild, expected_space=destination["space_id"])
            def operate():
                service = CardReviews(self.registry, actor, destination["space_id"])
                if command == "card_show": return service.show(options["object_id"])
                return service.decide(options["object_id"], options["decision"], options["expected_version"], options["note"])
            return await asyncio.to_thread(operate)
        if command == "fork":
            answer = snowflake(options["answer_message_id"])
            if self.supervisor is None or self.fork_node is None: raise Unavailable()
            with self.registry.connect(readonly=True) as db:
                rows = db.execute("SELECT t.id,t.conversation_id FROM outbox o JOIN turns t ON t.id=o.turn_id JOIN conversations c ON c.id=t.conversation_id WHERE o.discord_message_id=? AND o.state='DELIVERED' AND t.status='ANSWERED' AND c.channel_id=? AND c.guild_id IS ?",
                    (answer, channel, guild)).fetchall()
                if len(rows) != 1: raise Unavailable()
                source = rows[0]
                self.registry._authorized(db, source["conversation_id"], actor, channel, guild,
                    statuses=("OPEN", "STOPPED", "NEEDS_ATTENTION"))
            async def authorize():
                await self.access.authorize(actor, channel_id=channel, guild_id=guild,
                    expected_space=destination["space_id"])
            ident = await self.fork_handler(self.supervisor, source_id=source["conversation_id"],
                turn_id=source["id"], actor=actor, channel_id=channel, guild_id=guild,
                request_id=message_id, name=options["name"], node=self.fork_node,
                sdk_module=self.fork_sdk, authorize=authorize)
            await authorize()
            self.registry.select_conversation(ident, actor, channel_id=channel, guild_id=guild)
            return self.registry.resolve_conversation(actor, channel_id=channel, guild_id=guild)
        if command in {"paper_thread", "paper_reconcile"}:
            if guild is None: raise ValueError("Paper threads require a shared guild channel")
            parent = destination["parent_channel_id"] or channel
            threads = PaperThreads(self.registry, self.access, self.rest, str(self.user.id),
                service_factory=self.absorption_factory)
            if command == "paper_reconcile":
                return await threads.reconcile(actor, guild, parent, destination["space_id"],
                    options["job_id"], options["starter_message_id"])
            return await threads.ensure(actor, guild, parent, destination["space_id"], options["job_id"])
        if command in {"paper_add", "paper_submission", "paper_retry"}:
            if self.submissions is None: raise Unavailable()
            ident = options.get("submission_id")
            if command == "paper_retry":
                self.submissions.retry(ident, actor, channel, guild, destination["space_id"])
            if command == "paper_add":
                ident = self.submissions.enqueue(actor, channel, guild, destination["space_id"], message_id,
                    options["url"], limits=SpendingLimits(options["max_calls"], options["max_reserved_tokens"]))
            return self.submissions.show(ident, actor, channel, guild, destination["space_id"])
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
                guild_id=guild, message_id=message_id, prompt=question, question_is_message=False)
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
            if self.feedback_task is not None and self.feedback_task.done():
                try: self.feedback_task.result()
                except Exception: pass
                self.feedback_task = None
            if self.feedback_task is None:
                self.feedback_task = asyncio.create_task(self.feedback_reconciler.work_once())
            if self.reconcile_task is not None and self.reconcile_task.done():
                try: self.reconcile_task.result()
                except Exception: pass
                self.reconcile_task = None
            if self.reconcile_task is None:
                self.reconcile_task = asyncio.create_task(self.reconciler.work_once())
            if self.source_task is not None and self.source_task.done():
                try: self.source_task.result()
                except Exception: pass
                self.source_task = None
            if self.source_task is None:
                self.source_task = asyncio.create_task(self.submissions.work_once())
            if self.discussion_task is not None and self.discussion_task.done():
                try: self.discussion_task.result()
                except Exception: pass
                self.discussion_task = None
            if self.discussion_task is None:
                self.discussion_task = asyncio.create_task(self.discussion_worker.work_once())
            if self.thread_task is not None and self.thread_task.done():
                try: self.thread_task.result()
                except Exception: pass
                self.thread_task = None
            if self.thread_task is None:
                self.thread_task = asyncio.create_task(self.thread_worker.work_once())
            if self.paid_task is not None and self.paid_task.done():
                try: self.paid_task.result()
                except Exception: pass
                self.paid_task = None
            if self.paid_task is None and self.run_approved_absorption:
                self.paid_task = asyncio.create_task(self.paid_worker.work_once())
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
        self.publication_stopping = True
        if self.discussion_worker: self.discussion_worker.stopping = True
        if self.thread_worker: self.thread_worker.stopping = True
        if self.paid_worker: self.paid_worker.stopping = True
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        if self.feedback_task:
            self.feedback_task.cancel()
            await asyncio.gather(self.feedback_task, return_exceptions=True)
        for task in self.running_turns: task.cancel()
        await asyncio.gather(*self.running_turns, return_exceptions=True)
        # A source ingest runs in a thread; cancelling its asyncio waiter would
        # not stop disk writes. Join it before releasing process ownership.
        if self.source_task: await asyncio.gather(self.source_task, return_exceptions=True)
        if self.thread_task: await asyncio.gather(self.thread_task, return_exceptions=True)
        if self.paid_task: await asyncio.gather(self.paid_task, return_exceptions=True)
        if self.discussion_task: await asyncio.gather(self.discussion_task, return_exceptions=True)
        if self.reconcile_task:
            self.reconcile_task.cancel()
            await asyncio.gather(self.reconcile_task, return_exceptions=True)
        if self.publication_tasks: await asyncio.gather(*self.publication_tasks, return_exceptions=True)
        if self.supervisor: await self.supervisor.close()
        if self.rest: await self.rest.aclose()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description="Foreground Arms Discord Gateway")
    parser.add_argument("--root", required=True, help="Existing Arms operational directory")
    parser.add_argument("--spaces-root", required=True, help="Existing Brain space registry")
    parser.add_argument("--pi", required=True, help="Absolute Pi executable")
    parser.add_argument("--fork-node", help="Trusted absolute Node executable for session branching (defaults to local node)")
    parser.add_argument("--fork-sdk", help="Trusted absolute Pi SDK index.js (defaults beside the resolved Pi executable)")
    parser.add_argument("--connect", action="store_true", help="Explicitly connect to Discord")
    parser.add_argument("--sync-commands", action="store_true", help="Replace this bot application's global commands")
    parser.add_argument("--run-approved-absorption", action="store_true", help="Execute explicitly approved scoped absorption jobs")
    args = parser.parse_args()
    if not args.connect: parser.error("--connect is required; no connection made")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token: parser.error("DISCORD_BOT_TOKEN is required in the backend environment")
    registry = ArmsRegistry(args.root, SpaceRegistry(args.spaces_root))
    client = ResearchGateway(registry, args.pi, sync_commands=args.sync_commands,
                             run_approved_absorption=args.run_approved_absorption,
                             fork_node=args.fork_node, fork_sdk=args.fork_sdk)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
