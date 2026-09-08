import asyncio
import pytest

from research_arms.saves import save_excerpt
from research_arms import Unavailable
from test_registry import setup
from test_discussion_worker import answered, Access
from research_arms.discussion_worker import DiscussionWorker


def test_scoped_save_requires_current_projection_digest_and_maintainer(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    args = {"revision": 1, "start": 0, "end": 17}
    with pytest.raises(ValueError): save_excerpt(arms, "1", "21", "10", "200", **args)
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    preview = save_excerpt(arms, "1", "21", "10", "200", **args)
    digest = preview["result"]["digest"]
    with pytest.raises(Unavailable): save_excerpt(arms, "1", "30", None, "200", **args)
    with pytest.raises(PermissionError):
        save_excerpt(arms, "2", "21", "10", "200", **args, confirm=True, digest=digest)
    with pytest.raises(ValueError):
        save_excerpt(arms, "1", "21", "10", "200", **args, confirm=True, digest="0" * 64)
    result = save_excerpt(arms, "1", "21", "10", "200", **args, confirm=True, digest=digest)
    assert result["space_id"] == "project" and result["review_state"] == "UNREVIEWED"
    assert not save_excerpt(arms, "1", "21", "10", "200", **args, confirm=True, digest=digest)["created"]
    assert spaces.open("project").recall("recorded") == []
    assert spaces.open("project").search("recorded")
