import asyncio
import json

from research_arms.tool_bridge import ToolBridge
from test_registry import setup, shared, turn


async def call(bridge, token, **changes):
    reader, writer = await asyncio.open_unix_connection(bridge.path)
    request = {"token": token, "tool": "research_recall", "space_id": "project",
               "arguments": {"query": "PRIVATE_CANARY"}, **changes}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    result = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return result


def test_private_socket_scope_token_rotation_and_revocation(setup):
    arms, spaces = setup
    async def run():
        conv = shared(arms)
        ident = turn(arms, conv)
        arms.claim()
        bridge = await ToolBridge(arms).start()
        try:
            auth = bridge.bind(ident)
            assert bridge.path.parent.stat().st_mode & 0o777 == 0o700
            result = await call(bridge, auth["token"])
            assert result["ok"] and "PRIVATE_CANARY" not in json.dumps(result)
            for changes in [{"space_id": "alice"}, {"tool": "ingest"},
                            {"arguments": {"query": "x", "root": "/private"}},
                            {"principal": "alice"}]:
                result = await call(bridge, auth["token"], **changes)
                assert result == {"ok": False, "error": "Research resource unavailable"}
            assert not (await call(bridge, "guessed"))["ok"]
            bridge.unbind()
            assert not (await call(bridge, auth["token"]))["ok"]
            new = bridge.bind(ident)
            assert new["token"] != auth["token"]
            assert not (await call(bridge, auth["token"]))["ok"]
            spaces.set_membership("project", "bob", None)
            assert not (await call(bridge, new["token"]))["ok"]
        finally:
            path = bridge.path
            await bridge.close()
            assert not path.exists()
    asyncio.run(run())


def test_transport_revoked_between_read_and_tool_result(setup):
    arms, _ = setup
    async def run():
        ident = turn(arms, shared(arms))
        arms.claim()
        bridge = await ToolBridge(arms).start()
        checks = []
        async def authorize(turn_id):
            assert turn_id == ident
            checks.append(turn_id)
            if len(checks) == 2: raise PermissionError("Removed from Discord channel")
        bridge.authorize_turn = authorize
        try:
            token = bridge.bind(ident)["token"]
            assert await call(bridge, token) == {"ok": False, "error": "Research resource unavailable"}
            assert len(checks) == 2
        finally: await bridge.close()
    asyncio.run(run())


def test_discussion_tool_is_scoped_bounded_and_not_scientific_recall(setup):
    arms, spaces = setup
    payload = {"id": "history", "revision": 1, "conversation_id": "previous", "author": "alice",
        "question": "Contrast directions", "answer": "Prior discussion only", "guild_id": "10", "channel_id": "20",
        "message_id": "100", "answer_message_id": "101", "pi_entry_id": "entry", "deleted": False,
        "recorded_at": "2026-09-07T12:00:00+00:00"}
    spaces.open("project").record_discussion(payload)
    spaces.open("alice").record_discussion({**payload, "guild_id": None, "question": "PRIVATE_CANARY"})
    async def run():
        ident = turn(arms, shared(arms))
        arms.claim()
        bridge = await ToolBridge(arms).start()
        try:
            token = bridge.bind(ident)["token"]
            args = {"tool": "research_discussed", "arguments": {"query": "Contrast"}}
            result = await call(bridge, token, **args)
            assert result["ok"] and "Prior discussion only" in json.dumps(result)
            assert "NOT_SCIENTIFIC_MEMORY" in json.dumps(result)
            assert not (await call(bridge, token, **{**args, "space_id": "alice"}))["ok"]
            for bad in ({"query": "x", "limit": 4}, {"query": "x", "kinds": ["note"]}, {"query": "x" * 2001}):
                assert not (await call(bridge, token, tool="research_discussed", arguments=bad))["ok"]
            science = await call(bridge, token, arguments={"query": "Contrast"})
            assert "Prior discussion only" not in json.dumps(science)
            spaces.set_membership("project", "bob", None)
            assert not (await call(bridge, token, **args))["ok"]
        finally: await bridge.close()
    asyncio.run(run())
