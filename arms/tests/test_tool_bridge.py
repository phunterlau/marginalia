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
