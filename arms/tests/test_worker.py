import asyncio
import json

import pytest

from research_arms.worker import Supervisor, final_answer
from research_arms.pi_rpc import PiProtocolError
from test_registry import setup, shared, turn


class Bridge:
    def __init__(self, registry):
        self.auth_path = registry.path
        self.turn = None
    async def start(self): return self
    def bind(self, turn): self.turn = turn
    def unbind(self): self.turn = None
    async def close(self): self.unbind()


class Client:
    def __init__(self, registry, fail=False):
        self.registry, self.fail = registry, fail
        self.prompts, self.closed = [], False
    async def command(self, name):
        with self.registry.connect(readonly=True) as db:
            entries = [{"id": row[0]} for row in db.execute("SELECT pi_entry_id FROM turns WHERE status='ANSWERED'")]
        return {"leafId": "previous", "entries": entries}
    async def prompt(self, message, since=None):
        assert since == "previous"
        with self.registry.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='pi_dispatched'").fetchone()[0] >= 1
        self.prompts.append(json.loads(message))
        if self.fail: raise asyncio.TimeoutError()
        return {"entries": [{"type": "message", "id": "answer_" + str(len(self.prompts)),
                 "message": {"role": "assistant", "stopReason": "stop",
                             "content": [{"type": "text", "text": "Source-qualified answer"}]}}]}
    async def close(self): self.closed = True


def test_worker_reuses_exact_session_and_persists_answer_before_delivery(setup):
    arms, _ = setup
    conv = shared(arms)
    first = turn(arms, conv)
    clients, paths = [], []
    async def factory(executable, directory, session, **kwargs):
        paths.append((directory, session))
        client = Client(arms)
        clients.append(client)
        return client
    async def run():
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        try:
            assert await supervisor.run_once() == first
            second = turn(arms, conv, "2", "101", anchor_turn_id=first)
            assert await supervisor.run_once() == second
            assert len(clients) == 1
            assert [p["author"] for p in clients[0].prompts] == ["alice", "bob"]
            assert clients[0].prompts[1]["quoted_reply_anchor"] == "Source-qualified answer"
            assert "PRIVATE_CANARY" not in json.dumps(clients[0].prompts)
            assert paths[0][0].is_relative_to(arms.root / "sessions")
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM outbox WHERE state='PENDING'").fetchone()[0] == 2
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='pi_dispatched'").fetchone()[0] == 2
        finally: await supervisor.close()
        assert clients[0].closed
    asyncio.run(run())


def test_failure_quarantines_followups_without_replay(setup):
    arms, _ = setup
    conv = shared(arms)
    turn(arms, conv)
    turn(arms, conv, message="101")
    client = Client(arms, fail=True)
    async def factory(*args, **kwargs): return client
    async def run():
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        with pytest.raises(asyncio.TimeoutError): await supervisor.run_once()
        assert await supervisor.run_once() is None
        assert client.closed
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM turns WHERE status='NEEDS_ATTENTION'").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        await supervisor.close()
    asyncio.run(run())


def test_idle_and_revoked_workers_retire(setup):
    arms, spaces = setup
    conv = shared(arms)
    turn(arms, conv)
    clients = []
    async def factory(*args, **kwargs):
        client = Client(arms)
        clients.append(client)
        return client
    async def run():
        clock = [0]
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory,
                                bridge_factory=Bridge, clock=lambda: clock[0])
        await supervisor.run_once()
        clock[0] = 601
        await supervisor.maintain()
        assert clients[0].closed
        turn(arms, conv, message="101")
        await supervisor.run_once()
        spaces.set_membership("project", "bob", None)
        await supervisor.maintain()
        assert clients[1].closed and not supervisor.workers
        await supervisor.close()
    asyncio.run(run())


@pytest.mark.parametrize("stop", ["error", "aborted", "length", "toolUse"])
def test_incomplete_answers_are_not_deliverable(stop):
    with pytest.raises(PiProtocolError):
        final_answer({"entries": [{"type": "message", "id": "a", "message": {
            "role": "assistant", "stopReason": stop, "content": [{"type": "text", "text": "partial"}]}}]})


def test_single_supervisor_and_recovery_required(setup):
    arms, _ = setup
    async def run():
        supervisor = Supervisor(arms, "/unused/pi")
        with pytest.raises(BlockingIOError): Supervisor(arms, "/unused/pi")
        await supervisor.close()
        conv = shared(arms)
        turn(arms, conv)
        arms.claim()
        with pytest.raises(RuntimeError, match="recovery"):
            Supervisor(arms, "/unused/pi")
    asyncio.run(run())


def test_missing_session_history_fails_before_new_dispatch(setup):
    arms, _ = setup
    conv = shared(arms)
    first = turn(arms, conv)
    arms.claim()
    arms.save_answer(first, "Prior answer", "persisted_entry")
    turn(arms, conv, message="101")
    class Empty(Client):
        async def command(self, name): return {"leafId": None, "entries": []}
    client = Empty(arms)
    async def factory(*args, **kwargs): return client
    async def run():
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        try:
            with pytest.raises(PiProtocolError, match="Prior answer missing"):
                await supervisor.run_once()
            assert not client.prompts
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='pi_dispatched'").fetchone()[0] == 0
        finally: await supervisor.close()
    asyncio.run(run())


def test_transport_permission_denial_prevents_pi_launch(setup):
    arms, _ = setup
    turn(arms, shared(arms))
    async def deny(ident): raise PermissionError("Transport access removed")
    async def launch(*args, **kwargs): raise AssertionError("Must not launch")
    async def run():
        supervisor = Supervisor(arms, "/unused/pi", client_factory=launch, authorize_turn=deny)
        try:
            with pytest.raises(PermissionError): await supervisor.run_once()
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='pi_dispatched'").fetchone()[0] == 0
        finally: await supervisor.close()
    asyncio.run(run())


def test_two_active_workers_and_revocation_during_prompt(setup):
    arms, spaces = setup
    for message in ["100", "101", "102"]:
        turn(arms, shared(arms), message=message)
    clients = []
    async def run():
        entered = asyncio.Event()
        released = asyncio.Event()
        class Waiting(Client):
            async def prompt(self, *args, **kwargs):
                if len(clients) == 2: entered.set()
                await released.wait()
                return await super().prompt(*args, **kwargs)
            async def close(self):
                await super().close()
                released.set()
        async def factory(*args, **kwargs):
            client = Waiting(arms)
            clients.append(client)
            return client
        supervisor = Supervisor(arms, "/unused/pi", client_factory=factory, bridge_factory=Bridge)
        tasks = [asyncio.create_task(supervisor.run_once()) for _ in range(2)]
        await asyncio.wait_for(entered.wait(), 2)
        assert await supervisor.run_once() is None
        assert len(clients) == 2
        spaces.set_membership("project", "bob", None)
        await supervisor.maintain()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, Exception) for result in results)
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        await supervisor.close()
    asyncio.run(run())
