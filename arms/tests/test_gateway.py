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
        assert {cmd.name for cmd in client.tree.get_commands()} == {"new", "resume", "session", "space", "ask", "stop"}
        assert client.intents.guilds and not client.intents.message_content and not client.intents.members
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
