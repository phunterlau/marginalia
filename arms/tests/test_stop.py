import asyncio

import pytest

from research_arms import Unavailable
from research_arms.worker import Supervisor
from test_registry import setup, shared, turn
from test_worker import Bridge, Client


def test_stop_cancels_queue_without_poisoning_idle_session(setup):
    arms, _ = setup
    conv = shared(arms)
    turn(arms, conv)
    result = arms.stop_conversation(conv, "1", channel_id="21", guild_id="10")
    assert result == {"cancelled": 1, "interrupted": 0, "requires_recovery": False}
    assert arms.claim() is None
    assert turn(arms, conv, message="101")


def test_stop_checks_identity_and_blocks_interrupted_reuse(setup):
    arms, _ = setup
    conv = arms.new_conversation("1", channel_id="30")
    ident = arms.enqueue(conv, "1", channel_id="30", message_id="100", prompt="private")
    assert arms.claim() == ident
    for user, channel, guild in [("2", "30", None), ("1", "21", "10")]:
        with pytest.raises(Unavailable):
            arms.stop_conversation(conv, user, channel_id=channel, guild_id=guild)
    assert arms.stop_conversation(conv, "1", channel_id="30")["requires_recovery"]
    with pytest.raises(Unavailable): arms.save_answer(ident, "late answer", "entry")
    with pytest.raises(Unavailable):
        arms.enqueue(conv, "1", channel_id="30", message_id="101", prompt="new")
    arms.stop_conversation(conv, "1", channel_id="30")
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='conversation_stopped'").fetchone()[0] == 1


def test_stop_aborts_running_pi_and_discards_late_answer(setup):
    arms, _ = setup
    conv = shared(arms)
    turn(arms, conv)
    turn(arms, conv, message="101")
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Active(Client):
            aborted = False
            async def prompt(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super().prompt(*args, **kwargs)
            async def abort(self):
                self.aborted = True
                release.set()
        client = Active(arms)
        async def factory(*args, **kwargs): return client
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        task = asyncio.create_task(supervisor.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            result = await supervisor.stop(conv, "1", channel_id="21", guild_id="10")
            assert result == {"cancelled": 1, "interrupted": 1, "requires_recovery": True}
            with pytest.raises(Unavailable): await task
            assert client.aborted and client.closed
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        finally: await supervisor.close()
    asyncio.run(run())
