import asyncio
import copy
import json

import pytest

from research_arms import Unavailable
from research_arms.discord_access import DiscordAccess, channel_permissions, VIEW, SEND, HISTORY, ATTACH, THREAD_SEND, ADMIN
from test_registry import setup


def test_permission_overwrite_precedence_and_timeout():
    roles = [{"id": "10", "permissions": str(VIEW | SEND | HISTORY)}, {"id": "40", "permissions": "0"}, {"id": "41", "permissions": "0"}]
    member = {"user": {"id": "1"}, "roles": ["40", "41"]}
    overwrites = [{"id": "10", "type": 0, "deny": str(SEND), "allow": "0"},
                  {"id": "40", "type": 0, "deny": str(SEND), "allow": "0"},
                  {"id": "41", "type": 0, "deny": "0", "allow": str(SEND)}]
    assert channel_permissions("10", "99", member, roles, overwrites) & SEND
    overwrites.append({"id": "1", "type": 1, "deny": str(SEND), "allow": "0"})
    assert not channel_permissions("10", "99", member, roles, overwrites) & SEND
    member["communication_disabled_until"] = "2099-01-01T00:00:00Z"
    assert not channel_permissions("10", "99", member, roles, []) & SEND
    roles[1]["permissions"] = str(ADMIN)
    assert channel_permissions("10", "99", member, roles, overwrites) & SEND


def responses():
    return {
        "channels/21": {"id": "21", "guild_id": "10", "type": 11, "parent_id": "20", "thread_metadata": {"archived": False, "locked": False}},
        "channels/20": {"id": "20", "guild_id": "10", "type": 0, "permission_overwrites": []},
        "guilds/10": {"id": "10", "owner_id": "99"},
        "guilds/10/roles": [{"id": "10", "permissions": str(VIEW | HISTORY | THREAD_SEND | ATTACH)}],
        "guilds/10/members/1": {"user": {"id": "1"}, "roles": []},
        "guilds/10/members/123": {"user": {"id": "123"}, "roles": []},
    }


class Client:
    def __init__(self, data): self.data = data
    async def get(self, route):
        item = copy.deepcopy(self.data.get(route))
        class Response:
            status_code = 200 if item is not None else 404
            content = json.dumps(item).encode()
            def json(self): return item
        return Response()


def test_thread_access_is_fresh_and_parent_bound(setup):
    arms, _ = setup
    data = responses()
    async def run():
        access = DiscordAccess(arms, Client(data), "123")
        result = await access.authorize("1", channel_id="21", guild_id="10")
        assert result["space_id"] == "project" and result["parent_channel_id"] == "20"
        # Removing send-in-thread access must override an earlier successful read.
        data["guilds/10/roles"][0]["permissions"] = str(VIEW | HISTORY | ATTACH | SEND)
        with pytest.raises(Unavailable): await access.authorize("1", channel_id="21", guild_id="10")
    asyncio.run(run())


def test_thread_creation_requires_bot_permission(setup):
    arms, _ = setup
    data = responses()
    data["guilds/10/roles"][0]["permissions"] = str(VIEW | HISTORY | ATTACH | SEND)
    async def run():
        access = DiscordAccess(arms, Client(data), "123")
        await access.authorize("1", channel_id="20", guild_id="10")
        with pytest.raises(Unavailable):
            await access.authorize("1", channel_id="20", guild_id="10", require_thread_creation=True)
        data["channels/20"]["permission_overwrites"] = [
            {"id": "123", "type": 1, "allow": str(1 << 35), "deny": "0"}]
        await access.authorize("1", channel_id="20", guild_id="10", require_thread_creation=True)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["guild", "parent", "archived", "private", "member"])
def test_thread_mismatches_fail_closed(setup, change):
    arms, _ = setup
    data = responses()
    if change == "guild": data["channels/21"]["guild_id"] = "11"
    if change == "parent": data["channels/20"]["id"] = "22"
    if change == "archived": data["channels/21"]["thread_metadata"]["archived"] = True
    if change == "private": data["channels/21"]["type"] = 12
    if change == "member": data.pop("guilds/10/members/1")
    async def run():
        with pytest.raises(Unavailable): await DiscordAccess(arms, Client(data), "123").authorize("1", channel_id="21", guild_id="10")
    asyncio.run(run())


def test_dm_requires_exact_recipient_and_personal_space(setup):
    arms, _ = setup
    data = {"channels/30": {"id": "30", "type": 1, "recipients": [{"id": "1"}]}}
    async def run():
        access = DiscordAccess(arms, Client(data), "123")
        assert (await access.authorize("1", channel_id="30"))["space_id"] == "alice"
        with pytest.raises(Unavailable): await access.authorize("2", channel_id="30")
        with pytest.raises(Unavailable): await access.authorize("1", channel_id="30", expected_space="project")
        data["channels/30"]["type"] = 3
        with pytest.raises(Unavailable): await access.authorize("1", channel_id="30")
    asyncio.run(run())
