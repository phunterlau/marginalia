from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from research_brain import Brain
from research_brain.spaces import SpaceRegistry
from research_arms import ArmsRegistry, Unavailable


@pytest.fixture
def setup(tmp_path):
    spaces = SpaceRegistry(tmp_path / "spaces", create=True)
    for name, kind, owner in [("alice", "personal", "alice"), ("bob", "personal", "bob"),
                              ("project", "shared", "alice")]:
        brain = Brain(tmp_path / name)
        brain.create_research_object(kind="note", body="PRIVATE_CANARY" if name == "alice" else name)
        spaces.register(name, kind=kind, owner=owner, root=brain.root)
    spaces.set_membership("project", "bob", "member")
    arms = ArmsRegistry(tmp_path / "arms", spaces, create=True)
    arms.register_principal("1", "alice", "alice")
    arms.register_principal("2", "bob", "bob")
    arms.bind_channel("10", "20", "project")
    return arms, spaces


def shared(arms):
    return arms.new_conversation("1", guild_id="10", channel_id="21", parent_channel_id="20")


def turn(arms, conversation, author="1", message="100", **kwargs):
    return arms.enqueue(conversation, author, guild_id="10", channel_id="21", message_id=message,
                        prompt="Research question", **kwargs)


def test_shared_authors_share_session_but_not_private_scope(setup):
    arms, _ = setup
    conv = shared(arms)
    a, b = turn(arms, conv), turn(arms, conv, "2", "101")
    assert arms.claim() == a
    assert arms.claim() is None
    with pytest.raises(Unavailable):
        arms.read(a, "alice", "search", "PRIVATE_CANARY")
    assert "PRIVATE_CANARY" not in json.dumps(arms.read(a, "project", "search", "PRIVATE_CANARY"))
    arms.save_answer(a, "Shared answer", "entry_1")
    assert arms.claim() == b
    with arms.connect(readonly=True) as db:
        scopes = [json.loads(r[0]) for r in db.execute("SELECT scope_json FROM turns ORDER BY created_at")]
        assert [s["principal"] for s in scopes] == ["alice", "bob"]
        assert len({s["conversation_id"] for s in scopes}) == 1
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_private_conversation_cannot_be_guessed_or_used_in_shared_destination(setup):
    arms, _ = setup
    conv = arms.new_conversation("1", channel_id="30", read_spaces=("project",))
    for author, channel, guild in [("2", "30", None), ("1", "21", "10")]:
        with pytest.raises(Unavailable):
            arms.enqueue(conv, author, channel_id=channel, guild_id=guild, message_id="1", prompt="x")
    with pytest.raises(Unavailable):
        arms.new_conversation("1", guild_id="10", channel_id="20", read_spaces=("alice",))
    with pytest.raises(Unavailable):
        arms.bind_channel("10", "25", "alice")


def test_revoke_invalidates_tools_answer_and_queued_work(setup):
    arms, spaces = setup
    conv = shared(arms)
    a, b = turn(arms, conv), turn(arms, conv, "2", "101")
    assert arms.claim() == a
    spaces.set_membership("project", "bob", None)
    with pytest.raises(Unavailable):
        arms.read(a, "project", "search", "x")
    with pytest.raises(Unavailable):
        arms.save_answer(a, "must not publish stale context", "entry_1")
    # Running work is not silently recycled. A future supervisor must stop it.
    assert arms.claim() is None
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    with pytest.raises(Unavailable):
        turn(arms, conv, "1", "103")
    assert len(arms.revoke_stale()) == 1
    assert arms.revoke_stale() == []
    with arms.connect(readonly=True) as db:
        assert {r[0] for r in db.execute("SELECT status FROM turns")} == {"REVOKED"}


def test_duplicate_delivery_events_and_concurrent_claims(setup):
    arms, _ = setup
    conv = shared(arms)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: turn(arms, conv), range(4)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: arms.claim(), range(4)))
    assert sum(c is not None for c in claims) == 1
    with pytest.raises(ValueError, match="changed"):
        arms.enqueue(conv, "1", guild_id="10", channel_id="21", message_id="100", prompt="edited")


def test_answer_and_outbox_are_atomic_and_restart_preserves_ids(setup):
    arms, spaces = setup
    conv = shared(arms)
    ident = turn(arms, conv)
    arms.claim()
    with arms.connect() as db:
        db.execute("CREATE TRIGGER break_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception, match="injected"):
        arms.save_answer(ident, "result", "entry_1")
    with arms.connect() as db:
        assert db.execute("SELECT status,answer FROM turns WHERE id=?", (ident,)).fetchone()[:] == ("RUNNING", None)
        db.execute("DROP TRIGGER break_outbox")
    reopened = ArmsRegistry(arms.root, spaces)
    reopened.save_answer(ident, "result", "entry_1")
    with reopened.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    with pytest.raises(Unavailable):
        reopened.save_answer(ident, "duplicate", "entry_1")


def test_anchor_and_capacity_boundaries(setup):
    arms, _ = setup
    conv = shared(arms)
    first = turn(arms, conv)
    with pytest.raises(Unavailable):
        turn(arms, conv, message="102", anchor_turn_id=first)
    arms.claim()
    arms.save_answer(first, "answer", "entry_1")
    turn(arms, conv, message="102", anchor_turn_id=first)
    other = shared(arms)
    with pytest.raises(Unavailable):
        turn(arms, other, message="103", anchor_turn_id=first)
    turn(arms, other, message="104")
    third = shared(arms)
    turn(arms, third, message="105")
    assert arms.claim() is not None
    assert arms.claim() is not None
    assert arms.claim() is None


def test_missing_registry_fails_closed(tmp_path):
    spaces = SpaceRegistry(tmp_path / "spaces", create=True)
    with pytest.raises(Exception):
        ArmsRegistry(tmp_path / "missing", spaces)
    assert not (tmp_path / "missing").exists()


def test_tool_bounds_and_no_provider_or_filesystem_parameters(setup):
    arms, _ = setup
    conv = shared(arms)
    ident = turn(arms, conv)
    arms.claim()
    for kwargs in [{"limit": 1000}, {"limit": True}, {"semantic_live": True}, {"root": "/tmp"}]:
        with pytest.raises(ValueError):
            arms.read(ident, "project", "search", "x", **kwargs)
    with pytest.raises(ValueError):
        arms.read(ident, "project", "get_document", "/private/path")
    with pytest.raises(Unavailable):
        arms.read(ident, "project", "ingest", "https://arxiv.org/abs/2305.18290")
