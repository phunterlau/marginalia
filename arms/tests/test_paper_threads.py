import asyncio
from types import SimpleNamespace

import pytest

from research_arms.paper_threads import PaperThreads
from test_registry import setup


class Access:
    async def authorize(self, *args, **kwargs): return {}


class Service:
    revision = "v2"
    def __init__(self, *args):
        self.jobs = SimpleNamespace(
            show=lambda ident: {"plan": {"document_id": "doc_test", "source": {"version_label": self.revision}}},
            brain=SimpleNamespace(get_document=lambda ident: {"title": "Synthetic paper"}))


class Client:
    def __init__(self, fail=False): self.calls, self.fail, self.message_id = [], fail, 299
    async def post(self, path, *, json):
        self.calls.append((path, json))
        if self.fail: raise asyncio.TimeoutError()
        if path.endswith("/messages"): self.message_id += 1
        item = ({"id": str(self.message_id), "channel_id": "20", "author": {"id": "123"}, "nonce": json["nonce"]}
                if path.endswith("/messages") else {"id": str(self.message_id), "guild_id": "10", "parent_id": "20", "type": 11})
        return SimpleNamespace(status_code=200, json=lambda: item)


def test_pinned_thread_reuses_exact_revision_and_persists_checkpoints(setup):
    arms, _ = setup
    client = Client()
    async def run():
        threads = PaperThreads(arms, Access(), client, "123", service_factory=Service)
        first = await threads.ensure("1", "10", "20", "project", "job_x")
        second = await threads.ensure("1", "10", "20", "project", "job_x")
        assert first["thread_id"] == second["thread_id"] == "300"
        assert len(client.calls) == 2
        assert arms.resolve_conversation("2", channel_id="300", guild_id="10")["conversation_id"] == first["conversation_id"]
        assert client.calls[0][1]["allowed_mentions"]["parse"] == []
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT state FROM paper_threads").fetchone()[0] == "COMPLETE"
    asyncio.run(run())


def test_distinct_revisions_have_distinct_sessions(setup):
    arms, _ = setup
    class NewRevision(Service): revision = "v3"
    async def run():
        client = Client()
        older = await PaperThreads(arms, Access(), client, "123", service_factory=Service).ensure("1", "10", "20", "project", "job_old")
        newer = await PaperThreads(arms, Access(), client, "123", service_factory=NewRevision).ensure("1", "10", "20", "project", "job_new")
        assert older["thread_id"] != newer["thread_id"]
        assert older["conversation_id"] != newer["conversation_id"]
        with arms.connect(readonly=True) as db:
            assert {row[0] for row in db.execute("SELECT revision FROM paper_threads")} == {"v2", "v3"}
    asyncio.run(run())


def test_unknown_message_send_never_repeats(setup):
    arms, _ = setup
    client = Client(fail=True)
    async def run():
        threads = PaperThreads(arms, Access(), client, "123", service_factory=Service)
        with pytest.raises(asyncio.TimeoutError): await threads.ensure("1", "10", "20", "project", "job_x")
        with pytest.raises(ValueError): await threads.ensure("1", "10", "20", "project", "job_x")
        assert len(client.calls) == 1
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT state FROM paper_threads").fetchone()[0] == "NEEDS_ATTENTION"
    asyncio.run(run())


class LostThreadResponse(Client):
    def __init__(self, corrupt=None):
        super().__init__()
        self.corrupt, self.remote = corrupt, {}
    async def post(self, path, *, json):
        result = await super().post(path, json=json)
        self.remote[("channels/20/messages/300" if path.endswith("/messages") else "channels/300")] = result.json()
        if path.endswith("/threads"): raise asyncio.TimeoutError()
        return result
    async def get(self, path):
        result = dict(self.remote[path])
        if self.corrupt == "nonce" and "nonce" in result: result["nonce"] = "123"
        if self.corrupt == "no_nonce": result.pop("nonce", None)
        if self.corrupt == "parent" and "parent_id" in result: result["parent_id"] = "99"
        if self.corrupt == "author" and "author" in result: result["author"] = {"id": "999"}
        return SimpleNamespace(status_code=404 if self.corrupt == "missing" else 200, json=lambda: result)


def test_reconcile_existing_thread_is_read_only_remote_and_idempotent(setup):
    arms, _ = setup
    async def run():
        client = LostThreadResponse()
        threads = PaperThreads(arms, Access(), client, "123", service_factory=Service)
        with pytest.raises(asyncio.TimeoutError): await threads.ensure("1", "10", "20", "project", "job_x")
        recovered = await threads.reconcile("1", "10", "20", "project", "job_x", "300")
        replay = await threads.reconcile("1", "10", "20", "project", "job_x", "300")
        assert recovered["conversation_id"] == replay["conversation_id"]
        assert len(client.calls) == 2
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='paper_thread_complete'").fetchone()[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize("corrupt", ["nonce", "no_nonce", "parent", "author", "missing"])
def test_reconcile_rejects_unverified_remote_resources(setup, corrupt):
    from research_arms import Unavailable
    arms, _ = setup
    async def run():
        client = LostThreadResponse(corrupt)
        threads = PaperThreads(arms, Access(), client, "123", service_factory=Service)
        with pytest.raises(asyncio.TimeoutError): await threads.ensure("1", "10", "20", "project", "job_x")
        with pytest.raises(Unavailable): await threads.reconcile("1", "10", "20", "project", "job_x", "300")
        assert len(client.calls) == 2
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
            assert db.execute("SELECT state FROM paper_threads").fetchone()[0] == "NEEDS_ATTENTION"
    asyncio.run(run())
