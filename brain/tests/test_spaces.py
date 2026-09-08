from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from research_brain import Brain
from research_brain.cli import main
from research_brain.spaces import SpaceRegistry


@pytest.fixture
def registry(tmp_path):
    registry = SpaceRegistry(tmp_path / "registry", create=True)
    for name, kind, owner in (("personal", "personal", "alice"), ("bob", "personal", "bob"), ("project", "shared", "alice")):
        root = tmp_path / name
        Brain(root)
        registry.register(name, kind=kind, owner=owner, root=root)
    return registry


def test_shared_cannot_attach_private_even_owner(registry):
    with pytest.raises(PermissionError):
        registry.scope("alice", conversation_id="c1", writable_space="project", read_spaces=("personal",))
    with pytest.raises(PermissionError):
        registry.scope("bob", conversation_id="c1", writable_space="personal")
    scope = registry.scope("alice", conversation_id="dm", writable_space="personal", read_spaces=("project",))
    assert scope.read_spaces == ("personal", "project")
    assert scope.digest != replace(scope, conversation_id="other").digest


def test_revocation_and_policy_changes_invalidate_scope(registry):
    registry.set_membership("project", "bob", "member")
    scope = registry.scope("bob", conversation_id="thread", writable_space="project")
    registry.validate(scope)
    with pytest.raises(PermissionError):
        registry.validate(scope, maintainer=True)
    registry.set_membership("project", "bob", None)
    with pytest.raises(PermissionError):
        registry.validate(scope)


def test_no_global_id_resolution_or_provider_calls(registry):
    private = registry.open("personal").create_research_object(kind="note", body="PRIVATE_CANARY_727")
    scope = registry.scope("alice", conversation_id="shared", writable_space="project")
    assert registry.read(scope, "project", "get_research_object", private.id)["result"] is None
    with pytest.raises(PermissionError):
        registry.read(scope, "personal", "get_research_object", private.id)
    for operation, kwargs in (("ingest", {}), ("search", {"semantic_live": True})):
        with pytest.raises(PermissionError):
            registry.read(scope, "project", operation, "anything", **kwargs)


def test_path_identifiers_and_overlapping_roots_rejected(registry):
    with pytest.raises(ValueError):
        registry.get("../personal")
    with pytest.raises(ValueError):
        registry.register("alias", kind="shared", owner="alice", root=registry.get("personal")["root"])
    with pytest.raises(ValueError):
        registry.set_membership("personal", "bob", "member")


def test_open_never_migrates_or_initializes(registry, tmp_path):
    root = registry.get("personal")["root"]
    with sqlite3.connect(f"{root}/brain.sqlite3") as db:
        db.execute("DELETE FROM schema_migrations WHERE version>=2")
    with pytest.raises(ValueError, match="Incompatible"):
        registry.open("personal")
    with sqlite3.connect(f"{root}/brain.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        Brain(tmp_path / "missing", initialize=False)
    assert not (tmp_path / "missing").exists()


def test_concurrent_registration_unique(registry, tmp_path):
    root = tmp_path / "new"
    Brain(root)
    def register(_):
        try:
            registry.register("new", kind="shared", owner="alice", root=root)
            return True
        except (ValueError, sqlite3.IntegrityError):
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register, range(2))) == [False, True]


def test_cli_space_is_explicit_and_empty(tmp_path, capsys):
    base = ["--registry", str(tmp_path / "registry")]
    assert main(base + ["spaces", "create", "project", "--owner", "alice", "--kind", "shared", "--data-root", str(tmp_path / "project")]) == 0
    assert main(base + ["--space", "project", "search", "anything"]) == 0
    assert main(base + ["--space", "unknown", "search", "anything"]) == 2
    assert not (tmp_path / "unknown").exists()
