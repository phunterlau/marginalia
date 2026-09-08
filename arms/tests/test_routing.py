import sqlite3

import pytest

from research_arms import ArmsRegistry, Unavailable
from research_arms.migrations import migrate_v1_to_v2, migrate_v2_to_v3
from test_registry import setup, shared, turn


def test_explicit_dm_selection_and_reply_anchor_survive_restart(setup):
    arms, spaces = setup
    first = arms.new_conversation("1", channel_id="30", name="First")
    second = arms.new_conversation("1", channel_id="30", name="Second")
    with pytest.raises(Unavailable): arms.resolve_conversation("1", channel_id="30")
    arms.select_conversation(first, "1", channel_id="30")
    ident = arms.enqueue(first, "1", channel_id="30", message_id="100", prompt="question")
    arms.claim()
    arms.save_answer(ident, "answer", "entry")
    delivery = arms.begin_delivery(ident)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    arms.select_conversation(second, "1", channel_id="30")
    arms = ArmsRegistry(arms.root, spaces)
    assert arms.resolve_conversation("1", channel_id="30")["conversation_id"] == second
    reply = arms.resolve_conversation("1", channel_id="30", reply_message_id="200")
    assert reply["conversation_id"] == first and reply["anchor_turn_id"] == ident
    assert arms.resolve_conversation("1", channel_id="30")["conversation_id"] == second
    for user, channel in [("2", "30"), ("1", "31")]:
        with pytest.raises(Unavailable): arms.resolve_conversation(user, channel_id=channel, reply_message_id="200")
    with pytest.raises(Unavailable): arms.resolve_conversation("1", channel_id="30", reply_message_id="999")


def test_shared_selection_is_channel_bound_and_revocation_fails_closed(setup):
    arms, spaces = setup
    conv = shared(arms)
    arms.select_conversation(conv, "1", channel_id="21", guild_id="10")
    assert arms.resolve_conversation("2", channel_id="21", guild_id="10")["conversation_id"] == conv
    with pytest.raises(Unavailable): arms.select_conversation(conv, "1", channel_id="30")
    spaces.set_membership("project", "bob", None)
    with pytest.raises(Unavailable): arms.resolve_conversation("2", channel_id="21", guild_id="10")


def test_explicit_backed_up_migration_preserves_v1(setup):
    arms, spaces = setup
    conv = shared(arms)
    ident = turn(arms, conv)
    with arms.connect() as db:
        db.execute("DROP TABLE conversation_routes")
        db.execute("DROP TABLE session_forks")
        db.execute("UPDATE meta SET version=1")
    with pytest.raises(ValueError, match="migration"): ArmsRegistry(arms.root, spaces)
    backup = migrate_v1_to_v2(arms.root)
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version FROM meta").fetchone()[0] == 1
        assert db.execute("SELECT id FROM turns").fetchone()[0] == ident
    assert migrate_v1_to_v2(arms.root) is None
    assert migrate_v2_to_v3(arms.root)
    reopened = ArmsRegistry(arms.root, spaces)
    reopened.select_conversation(conv, "1", channel_id="21", guild_id="10")
    assert migrate_v2_to_v3(arms.root) is None


def test_migration_failure_leaves_original_version_and_backup(setup):
    arms, _ = setup
    # Simulate an incompatible partial schema: CREATE must fail atomically.
    with arms.connect() as db: db.execute("UPDATE meta SET version=1")
    with pytest.raises(sqlite3.OperationalError): migrate_v1_to_v2(arms.root)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT version FROM meta").fetchone()[0] == 1
    assert len(list(arms.root.glob("arms-v1-backup-*.sqlite3"))) == 1
