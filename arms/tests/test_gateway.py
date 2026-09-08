import asyncio
from types import SimpleNamespace

import pytest
pytest.importorskip("discord")

from research_arms.gateway import ResearchGateway
from test_registry import setup


class Access:
    def __init__(self, denied=False): self.denied = denied
    async def authorize(self, actor, **kwargs):
        if self.denied: raise PermissionError()
        return {"actor": actor, "channel_id": "30", "guild_id": None,
                "parent_channel_id": None, "space_id": "alice", "audience": "personal:alice"}


class Interaction:
    def __init__(self, ident=100):
        self.id, self.user, self.channel_id, self.guild_id = ident, SimpleNamespace(id=1, bot=False), 30, None
        self.deferred, self.output = False, None
        self.response = self
    async def defer(self, **kwargs): self.deferred = True
    async def edit_original_response(self, **kwargs): self.output = kwargs["content"]


def test_native_command_registration_is_offline_and_minimal(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        assert {cmd.name for cmd in client.tree.get_commands()} == {"new", "resume", "fork", "fork-recover", "session", "space", "ask", "stop", "paper", "card", "publish", "discussed", "recall", "compare", "brainstorm", "save", "frontier", "interests", "deep-dive"}
        assert {cmd.name for cmd in client.tree.get_command("publish").commands} == {"prepare", "show", "consent", "approve", "cancel", "run"}
        assert {cmd.name for cmd in client.tree.get_command("card").commands} == {"show", "review"}
        assert {cmd.name for cmd in client.tree.get_command("paper").commands} == {"status", "brief", "cards", "evidence", "job", "approve", "add", "submission", "thread", "reconcile"}
        assert client.intents.guilds and not client.intents.message_content and not client.intents.members
        assert client.intents.guild_reactions and client.intents.dm_reactions
        assert client.supervisor is None and client.pump is None
        await client.close()
    asyncio.run(run())


def test_new_and_ask_replays_do_not_duplicate_or_switch_turns(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = Access()
        destination = await client.access.authorize("1")
        first = await client.handle("new", "1", "30", None, "100", destination, name="First")
        assert await client.handle("new", "1", "30", None, "100", destination, name="First") == first
        question = await client.handle("ask", "1", "30", None, "101", destination, question="Research question")
        await client.handle("new", "1", "30", None, "102", destination, name="Second")
        replay = await client.handle("ask", "1", "30", None, "101", destination, question="Research question")
        assert replay["queued_turn"] == question["queued_turn"]
        assert replay["conversation_id"] == first["conversation_id"]
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
            assert db.execute("SELECT question_channel_id FROM turns").fetchone()[0] is None
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='question_interaction'").fetchone()[0] == 1
        await client.close()
    asyncio.run(run())


def test_unauthorized_interaction_never_creates_context(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access, client.supervisor = Access(denied=True), SimpleNamespace()
        interaction = Interaction()
        await client.execute(interaction, "new", name="Private name")
        assert interaction.deferred and "unavailable" in interaction.output
        assert "Private name" not in interaction.output
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        client.supervisor = None
        await client.close()
    asyncio.run(run())


def message(ident, *, guild=None, reference=None, mention=False, text="Follow-up"):
    return SimpleNamespace(id=ident, author=SimpleNamespace(id=1, bot=False), webhook_id=None,
        channel=SimpleNamespace(id=30), guild=SimpleNamespace(id=guild) if guild else None,
        reference=SimpleNamespace(message_id=reference) if reference else None,
        mentions=[SimpleNamespace(id=123)] if mention else [], content=text, attachments=[])


def test_dm_reply_uses_mapped_conversation_not_active_selection(setup):
    arms, _ = setup
    first = arms.new_conversation("1", channel_id="30")
    ident = arms.enqueue(first, "1", channel_id="30", message_id="100", prompt="First")
    arms.claim()
    arms.save_answer(ident, "answer", "entry")
    delivery = arms.begin_delivery(ident)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    second = arms.new_conversation("1", channel_id="30")
    arms.select_conversation(second, "1", channel_id="30")
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = Access()
        client._connection.user = SimpleNamespace(id=123)
        await client.on_message(message(300, reference=200))
        await client.on_message(message(300, reference=200))
        await client.on_message(message(301, reference=999))
        with arms.connect(readonly=True) as db:
            rows = db.execute("SELECT * FROM turns WHERE discord_message_id='300'").fetchall()
            assert len(rows) == 1 and rows[0]["conversation_id"] == first
            assert rows[0]["anchor_turn_id"] == ident
            assert db.execute("SELECT COUNT(*) FROM turns WHERE discord_message_id='301'").fetchone()[0] == 0
        assert arms.resolve_conversation("1", channel_id="30")["conversation_id"] == second
        await client.close()
    asyncio.run(run())


def test_casual_channel_messages_are_not_retained(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        client.access = Access()
        client._connection.user = SimpleNamespace(id=123)
        await client.on_message(message(300, guild=10, text="CASUAL_CANARY"))
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        await client.close()
    asyncio.run(run())


def test_parent_starter_reply_routes_to_paper_thread_without_switching_parent(setup):
    from research_arms.paper_threads import PaperThreads
    from test_paper_threads import Client, Service
    arms, _ = setup
    checks = []
    class SharedAccess:
        async def authorize(self, actor, **kwargs):
            checks.append(kwargs["channel_id"])
            return {"space_id": "project"}
    async def run():
        access = SharedAccess()
        paper = await PaperThreads(arms, access, Client(), "123", service_factory=Service).ensure("1", "10", "20", "project", "job_x")
        parent = arms.new_conversation("1", channel_id="20", guild_id="10")
        arms.select_conversation(parent, "1", channel_id="20", guild_id="10")
        client = ResearchGateway(arms, "/unused/pi")
        client.access = access
        client._connection.user = SimpleNamespace(id=123)
        incoming = message(400, guild=10, reference=300)
        incoming.channel.id = 20
        checks.clear()
        try:
            await client.on_message(incoming)
            await client.on_message(incoming)
            with arms.connect(readonly=True) as db:
                rows = db.execute("SELECT * FROM turns WHERE discord_message_id='400'").fetchall()
                assert len(rows) == 1 and rows[0]["conversation_id"] == paper["conversation_id"]
            assert checks[:3] == ["20", "300", "300"]
            assert arms.resolve_conversation("1", channel_id="20", guild_id="10")["conversation_id"] == parent
        finally:
            await client.close()
    asyncio.run(run())


def test_paper_approval_requires_confirmation_and_passes_exact_scope(setup):
    arms, _ = setup
    calls = []
    class Service:
        def __init__(self, registry, actor, space):
            assert registry is arms and actor == "1" and space == "alice"
        def preview(self, job): return {"job_id": job, "status": "WAITING_APPROVAL"}
        def approve(self, job, digest, *, live):
            calls.append((job, digest, live))
            return {"job_id": job, "status": "QUEUED"}
    async def run():
        client = ResearchGateway(arms, "/unused/pi", absorption_factory=Service)
        destination = {"space_id": "alice"}
        args = ("paper_approve", "1", "30", None, "100", destination)
        try:
            with pytest.raises(ValueError): await client.handle(*args, job_id="job_x", plan_digest="digest", confirm=False)
            assert calls == []
            result = await client.handle(*args, job_id="job_x", plan_digest="digest", confirm=True)
            assert result["status"] == "QUEUED" and calls == [("job_x", "digest", True)]
        finally: await client.close()
    asyncio.run(run())


def test_discussed_uses_destination_space_not_owners_personal_history(setup):
    from research_arms.discussion_worker import DiscussionWorker
    from test_discussion_worker import answered, Access as ProjectionAccess
    arms, _ = setup
    _, delivery = answered(arms, private=True)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        await DiscussionWorker(arms, ProjectionAccess()).work_once()
        client = ResearchGateway(arms, "/unused/pi")
        try:
            private = await client.handle("discussed", "1", "30", None, "300", {"space_id": "alice"}, question="PRIVATE_CANARY")
            shared = await client.handle("discussed", "1", "20", "10", "301", {"space_id": "project"}, question="PRIVATE_CANARY")
            assert len(private["result"]["items"]) == 1
            assert shared["result"]["items"] == []
        finally: await client.close()
    asyncio.run(run())


def test_brainstorm_creates_distinct_idempotent_conversation(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        try:
            old = arms.new_conversation("1", channel_id="30")
            destination = {"space_id": "alice", "parent_channel_id": None}
            first = await client.handle("brainstorm", "1", "30", None, "100", destination, question="Explore geometry")
            again = await client.handle("brainstorm", "1", "30", None, "100", destination, question="Explore geometry")
            assert first == again and first["conversation_id"] != old
            assert first["stage"] == "blind_first"
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='brainstorm_blind'").fetchone()[0] == 1
            with pytest.raises(ValueError):
                await client.handle("brainstorm", "1", "30", None, "100", destination, question="Changed")
        finally: await client.close()
    asyncio.run(run())


def test_interests_destination_and_explicit_private_attachments(setup):
    arms, _ = setup
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        try:
            personal = await client.handle("interests", "1", "30", None, "100", {"space_id": "alice"}, spaces="project")
            assert personal["audience"] == "personal:alice"
            project = await client.handle("interests", "1", "20", "10", "101", {"space_id": "project"})
            assert project["audience"] == "shared:project"
            with pytest.raises(ValueError):
                await client.handle("interests", "1", "20", "10", "102", {"space_id": "project"}, spaces="alice")
            with pytest.raises(PermissionError):
                await client.handle("interests", "2", "30", None, "103", {"space_id": "bob"}, spaces="alice")
        finally: await client.close()
    asyncio.run(run())


def test_raw_clear_and_message_delete_withdraw_feedback(setup):
    from research_arms.feedback import record, view
    from test_discussion_worker import answered
    from test_feedback import Emoji
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    scope = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    args = {"guild": "10", "channel": "21", "message": "200", "emoji": "🔥", "active": True}
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        try:
            payload = SimpleNamespace(guild_id=10, channel_id=21, message_id=200, emoji=Emoji())
            for handler in (client.on_raw_reaction_clear_emoji, client.on_raw_reaction_clear, client.on_raw_message_delete):
                record(arms, scope, **args)
                await handler(payload)
                assert not view(arms, scope, shared=True)["items"]
        finally: await client.close()
    asyncio.run(run())


def test_raw_delete_handlers_ignore_untracked_messages_and_deduplicate(setup):
    from test_discussion_worker import answered
    arms, _ = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        client = ResearchGateway(arms, "/unused/pi")
        try:
            await client.on_raw_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_id=999))
            await client.on_raw_bulk_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_ids={100, 999}))
            await client.on_raw_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_id=100))
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='discussion_message_deleted'").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 2
        finally: await client.close()
    asyncio.run(run())


def test_fork_routes_exact_delivered_answer_and_rejects_wrong_channel(setup):
    import uuid
    from research_arms.forks import fork_conversation
    from research_arms.worker import Supervisor
    from research_arms import Unavailable
    from test_forks import source_fixture
    arms, _ = setup
    source, turn = source_fixture(arms)
    delivery = arms.begin_delivery(turn)
    arms.confirm_delivery(delivery["delivery_id"], "500")
    calls = []
    async def factory(**kwargs):
        calls.append(kwargs)
        return {"session_id": str(uuid.uuid4())}
    async def handler(supervisor, **kwargs):
        return await fork_conversation(supervisor, **kwargs, fork_factory=factory)
    async def run():
        client = ResearchGateway(arms, "/unused/pi", fork_node="/unused/node", fork_handler=handler)
        client.supervisor = Supervisor(arms, "/unused/pi")
        client.access = Access()
        destination = {"space_id": "project"}
        args = ("fork", "1", "21", "10", "600", destination)
        try:
            first = await client.handle(*args, answer_message_id="500", name="Branch")
            second = await client.handle(*args, answer_message_id="500", name="Branch")
            assert first == second and first["conversation_id"] != source
            assert first["read_spaces"] == ["project"] and len(calls) == 1
            with pytest.raises(Unavailable):
                await client.handle("fork", "1", "30", None, "601", {"space_id": "alice"}, answer_message_id="500", name="Leak")
            assert len(calls) == 1
        finally: await client.close()
    asyncio.run(run())
