import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from research_arms import Unavailable
from research_arms.forks import fork_conversation
from research_arms.worker import Supervisor
from test_registry import setup, shared, turn


def source_fixture(arms):
    conv = shared(arms)
    ident = turn(arms, conv)
    arms.claim()
    arms.save_answer(ident, "completed", "entry")
    with arms.connect(readonly=True) as db:
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conv,)).fetchone()
    folder = arms.root / "sessions" / hashlib.sha256(b"project").hexdigest() / conv
    folder.mkdir(parents=True)
    (folder / ("synthetic_" + row["pi_session_id"] + ".jsonl")).write_text("synthetic fixture")
    return conv, ident


def test_fork_activation_and_idempotent_retry(setup):
    arms, _ = setup
    conv, ident = source_fixture(arms)
    calls = []
    async def factory(**kwargs):
        calls.append(kwargs)
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT status FROM conversations WHERE id!=?", (conv,)).fetchone()[0] == "FORKING"
        return {"session_id": str(uuid.uuid4())}
    async def run():
        supervisor = Supervisor(arms, "/unused/pi")
        try:
            args = dict(source_id=conv, turn_id=ident, actor="1", channel_id="21", guild_id="10",
                        request_id="request1", node="/unused/node", sdk_module="/unused/sdk", fork_factory=factory)
            child = await fork_conversation(supervisor, **args)
            assert await fork_conversation(supervisor, **args) == child
            assert len(calls) == 1
            arms.select_conversation(child, "2", channel_id="21", guild_id="10")
            scope = arms.resolve_conversation("2", channel_id="21", guild_id="10")
            assert scope["read_spaces"] == ["project"]
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM events WHERE kind='fork_completed'").fetchone()[0] == 1
        finally: await supervisor.close()
    asyncio.run(run())


@pytest.mark.parametrize("revoke", [False, True])
def test_partial_or_revoked_fork_never_activates_or_replays(setup, revoke):
    arms, spaces = setup
    conv, ident = source_fixture(arms)
    calls = []
    async def factory(**kwargs):
        calls.append(kwargs)
        if revoke:
            spaces.set_membership("project", "bob", None)
            return {"session_id": str(uuid.uuid4())}
        raise OSError("synthetic disk failure")
    async def run():
        supervisor = Supervisor(arms, "/unused/pi")
        try:
            args = dict(source_id=conv, turn_id=ident, actor="1", channel_id="21", guild_id="10",
                        request_id="request1", node="/unused/node", sdk_module="/unused/sdk", fork_factory=factory)
            with pytest.raises((OSError, Unavailable)): await fork_conversation(supervisor, **args)
            with pytest.raises((ValueError, Unavailable)): await fork_conversation(supervisor, **args)
            assert len(calls) == 1
            with arms.connect(readonly=True) as db:
                row = db.execute("SELECT * FROM session_forks").fetchone()
                assert row["state"] == "NEEDS_ATTENTION"
                assert db.execute("SELECT status FROM conversations WHERE id=?", (row["target_id"],)).fetchone()[0] != "OPEN"
        finally: await supervisor.close()
    asyncio.run(run())


def test_fork_rejects_wrong_destination_before_session_access(setup):
    arms, _ = setup
    conv, ident = source_fixture(arms)
    async def run():
        supervisor = Supervisor(arms, "/unused/pi")
        try:
            with pytest.raises(Unavailable):
                await fork_conversation(supervisor, source_id=conv, turn_id=ident, actor="1",
                    channel_id="30", request_id="x", node="/unused/node", sdk_module="/unused/sdk")
            with arms.connect(readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM session_forks").fetchone()[0] == 0
        finally: await supervisor.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("ARMS_TEST_PI"), reason="Optional installed Pi SDK test")
def test_native_coordinator_preserves_exact_completed_history(setup):
    arms, _ = setup
    conv = shared(arms)
    ident = turn(arms, conv)
    arms.claim()
    directory = arms.root / "sessions" / hashlib.sha256(b"project").hexdigest() / conv
    sdk = Path(os.environ["ARMS_TEST_PI"]).resolve().parent / "index.js"
    script = '''
      const {SessionManager} = await import(process.argv[1]);
      const m = SessionManager.create(process.argv[2],process.argv[2]);
      m.appendMessage({role:"user",content:"Question",timestamp:1});
      const entry=m.appendMessage({role:"assistant",content:[{type:"text",text:"Completed"}],stopReason:"stop",timestamp:2});
      m.appendMessage({role:"user",content:"FUTURE_CANARY",timestamp:3});
      console.log(JSON.stringify({entry,session:m.getSessionId()}));
    '''
    output = subprocess.run(["/opt/homebrew/bin/node", "--input-type=module", "-e", script,
        sdk.as_uri(), str(directory)], capture_output=True, text=True, check=True, timeout=20, env={})
    metadata = json.loads(output.stdout)
    arms.save_answer(ident, "Completed", metadata["entry"])
    with arms.connect() as db:
        db.execute("UPDATE conversations SET pi_session_id=? WHERE id=?", (metadata["session"], conv))
    async def run():
        supervisor = Supervisor(arms, os.environ["ARMS_TEST_PI"])
        try:
            child = await fork_conversation(supervisor, source_id=conv, turn_id=ident, actor="1",
                channel_id="21", guild_id="10", request_id="native", node="/opt/homebrew/bin/node", sdk_module=sdk)
            child_directory = directory.parent / child
            files = list(child_directory.glob("*.jsonl"))
            assert len(files) == 1
            assert "Completed" in files[0].read_text() and "FUTURE_CANARY" not in files[0].read_text()
            with arms.connect(readonly=True) as db:
                row = db.execute("SELECT * FROM conversations WHERE id=?", (child,)).fetchone()
                assert row["status"] == "OPEN" and row["pi_session_id"] != metadata["session"]
        finally: await supervisor.close()
    asyncio.run(run())
