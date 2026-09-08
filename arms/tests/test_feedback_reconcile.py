import asyncio
import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from research_arms.feedback_reconcile import reconcile, users, FeedbackReconciler
from research_arms.feedback import record, view
from test_registry import setup
from test_discussion_worker import answered


class Rest:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    async def get(self, route):
        self.calls.append(route)
        data = [{"id": "2", "bot": False}] if "🔥" in unquote(route) else []
        return SimpleNamespace(status_code=500 if self.fail and len(self.calls) == 8 else 200,
            content=json.dumps(data).encode(), json=lambda: data)


class Access:
    async def authorize(self, *args, **kwargs): return {}


def test_complete_snapshot_adds_missing_and_withdraws_absent_without_duplicates(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    scope = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    record(arms, scope, guild="10", channel="21", message="200", emoji="🔥", active=True)
    client = SimpleNamespace(registry=arms, access=Access(), rest=Rest(), user=SimpleNamespace(id=123))
    args = {"actor": "1", "guild": "10", "channel": "21", "message": "200"}
    first = asyncio.run(reconcile(client, **args))
    assert first == {"status": "COMPLETE", "changes": 2}
    assert asyncio.run(reconcile(client, **args))["changes"] == 0
    assert view(arms, scope, shared=True)["items"][0]["contributors"] == 1
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1


def test_late_failed_page_or_access_check_changes_nothing(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    scope = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    record(arms, scope, guild="10", channel="21", message="200", emoji="🔥", active=True)
    client = SimpleNamespace(registry=arms, access=Access(), rest=Rest(fail=True), user=SimpleNamespace(id=123))
    args = {"actor": "1", "guild": "10", "channel": "21", "message": "200"}
    with pytest.raises(PermissionError): asyncio.run(reconcile(client, **args))
    client.rest = Rest()
    async def deny(actor, **kwargs):
        if actor == "2": raise PermissionError()
    client.access.authorize = deny
    with pytest.raises(PermissionError): asyncio.run(reconcile(client, **args))
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='feedback_signal'").fetchone()[0] == 1


def test_repeated_full_page_fails_closed():
    class Repeated:
        async def get(self, route):
            data = [{"id": str(i)} for i in range(1, 101)]
            return SimpleNamespace(status_code=200, content=json.dumps(data).encode(), json=lambda: data)
    with pytest.raises(PermissionError): asyncio.run(users(Repeated(), "21", "200", "🔥", 0))


def test_scan_visits_answers_and_paper_starters_once_per_pass(setup, monkeypatch):
    from research_arms import feedback_reconcile
    arms, _ = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    with arms.connect() as db:
        conv = db.execute("SELECT id FROM conversations").fetchone()[0]
        db.execute("INSERT INTO paper_threads VALUES ('paper_test','project','10','20','doc_test','v1','COMPLETE','300','300',?,'2026-09-07')", (conv,))
    seen = []
    async def checked(client, **args):
        seen.append(args)
        if args["message"] == "200": raise PermissionError()
        return {"status": "COMPLETE", "changes": 0}
    monkeypatch.setattr(feedback_reconcile, "reconcile", checked)
    client = SimpleNamespace(registry=arms, feedback_lock=asyncio.Lock())
    scanner = FeedbackReconciler(client)
    async def run():
        assert await scanner.work_once() is None
        scanner.restart()
        assert (await scanner.work_once())["status"] == "NEEDS_ATTENTION"
        assert (await scanner.work_once())["status"] == "COMPLETE"
        assert await scanner.work_once() is None
        assert await scanner.work_once() is None
        scanner.restart()
        while await scanner.work_once() is not None: pass
    asyncio.run(run())
    assert [r["message"] for r in seen] == ["200", "300", "200", "300"]
    assert all(r["actor"] == "1" for r in seen)
