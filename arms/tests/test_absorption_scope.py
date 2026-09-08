from dataclasses import asdict

import pytest

from research_arms.absorption import ScopedAbsorption
from test_registry import setup


def test_only_owner_or_shared_maintainer_can_create_absorption_service(setup):
    arms, spaces = setup
    owner = ScopedAbsorption(arms, "1", "alice", create=True)
    owner._check(asdict(owner.scope))
    with pytest.raises(PermissionError): ScopedAbsorption(arms, "2", "alice", create=True)
    with pytest.raises(PermissionError): ScopedAbsorption(arms, "2", "project", create=True)
    assert not (spaces.open("project").root / "jobs.sqlite3").exists()
    spaces.set_membership("project", "bob", "maintainer")
    shared = ScopedAbsorption(arms, "2", "project", create=True)
    spaces.set_membership("project", "bob", "member")
    with pytest.raises(PermissionError): shared._check(asdict(shared.scope))
