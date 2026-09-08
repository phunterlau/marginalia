import asyncio
import json

import pytest

from research_arms import Unavailable
from research_arms.worker import Supervisor
from test_registry import setup
from test_worker import Bridge, Client


def test_blind_first_denies_all_memory_then_same_session_uses_fresh_scope(setup):
    from test_research_commands import paper
    arms, spaces = setup
    source = paper(spaces.open("alice"), "v1", "PAPER_CONTENT_CANARY")
    pin = source.document_id + "@v1"
    conv = arms.new_conversation("1", channel_id="30", blind_first=True, request_id="100", paper_pin=pin)
    first = arms.enqueue(conv, "1", channel_id="30", message_id="100", prompt="Independent ideas")
    clients = []
    class CheckedClient(Client):
        async def prompt(self, message, since=None):
            payload = json.loads(message)
            with arms.connect(readonly=True) as db:
                running = db.execute("SELECT id FROM turns WHERE status='RUNNING'").fetchone()[0]
            if payload["brainstorm_stage"] == "blind_first":
                for operation, argument in [("recall", "PRIVATE_CANARY"), ("search", "PRIVATE_CANARY"),
                    ("get_document", "doc_guess"), ("get_evidence", "block_guess"),
                    ("get_research_object", "obj_guess"), ("search_discussions", "PRIVATE_CANARY")]:
                    with pytest.raises(Unavailable): arms.read(running, "alice", operation, argument)
            else:
                assert arms.read(running, "alice", "recall", "PRIVATE_CANARY")["result"]
            return await super().prompt(message, since=since)
    async def factory(*args, **kwargs):
        client = CheckedClient(arms)
        clients.append(client)
        return client
    async def run():
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        try:
            assert await supervisor.run_once() == first
            second = arms.enqueue(conv, "1", channel_id="30", message_id="101", prompt="Now check memory", anchor_turn_id=first)
            assert await supervisor.run_once() == second
            assert len(clients) == 1
            assert [p["brainstorm_stage"] for p in clients[0].prompts] == ["blind_first", "memory_assisted"]
            assert "PRIVATE_CANARY" not in json.dumps(clients[0].prompts[0])
            assert "PAPER_CONTENT_CANARY" not in json.dumps(clients[0].prompts[0])
            assert all(p["paper_starting_point"]["revision"] == "v1" for p in clients[0].prompts)
            assert all(p["paper_starting_point"]["document_id"] == source.document_id for p in clients[0].prompts)
        finally: await supervisor.close()
    asyncio.run(run())


def test_brainstorm_mode_is_bound_to_request_and_pending_turns_stay_blind(setup):
    arms, _ = setup
    conv = arms.new_conversation("1", channel_id="30", blind_first=True, request_id="100")
    assert arms.new_conversation("1", channel_id="30", blind_first=True, request_id="100") == conv
    with pytest.raises(ValueError, match="changed"):
        arms.new_conversation("1", channel_id="30", request_id="100")
    for message in ("100", "101"):
        arms.enqueue(conv, "1", channel_id="30", message_id=message, prompt="Blind")
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='brainstorm_blind'").fetchone()[0] == 2


def test_paper_pin_is_scoped_immutable_and_invalid_creation_rolls_back(setup):
    from test_research_commands import paper
    arms, spaces = setup
    source = paper(spaces.open("alice"), "v1", "PRIVATE_PAPER_CANARY")
    pin = source.document_id + "@v1"
    conv = arms.new_conversation("1", channel_id="30", request_id="100", blind_first=True, paper_pin=pin)
    assert arms.new_conversation("1", channel_id="30", request_id="100", blind_first=True, paper_pin=pin) == conv
    with pytest.raises(ValueError, match="changed"):
        arms.new_conversation("1", channel_id="30", request_id="100", blind_first=True, paper_pin=source.document_id + "@v2")
    for kwargs in ({"channel_id": "20", "guild_id": "10", "paper_pin": pin},
                   {"channel_id": "30", "paper_pin": source.document_id + "@v2"}):
        with pytest.raises(Unavailable):
            arms.new_conversation("1", blind_first=True, **kwargs)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='brainstorm_paper'").fetchone()[0] == 1
