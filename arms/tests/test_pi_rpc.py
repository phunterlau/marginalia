import asyncio
import json
import sys
import uuid

import pytest

from research_arms.pi_rpc import PiRPC, PiProtocolError, launch_arguments


FAKE = r'''
import sys,json,time
for line in sys.stdin:
    request=json.loads(line)
    name=request['type']
    data={}
    if name=='get_state': data={'isStreaming':False,'pendingMessageCount':0}
    if name=='get_entries': data={'entries':[{'id':'entry_1','text':'line\u2028kept'}],'leafId':'entry_1'}
    print(json.dumps({'id':request['id'],'type':'response','command':name,'success':True,'data':data}),flush=True)
    if name=='prompt':
        print(json.dumps({'type':'agent_end','messages':[]}),flush=True)
        if request['message']=='hang': continue
        time.sleep(0.03)
        print(json.dumps({'type':'agent_settled'}),flush=True)
'''


async def fake(code=FAKE):
    process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=PiRPC.MAX_FRAME + 1)
    return PiRPC(process)


def test_exact_uuid_and_disabled_implicit_capabilities(tmp_path):
    args = launch_arguments("/opt/homebrew/bin/pi", tmp_path, str(uuid.uuid4()))
    for flag in ["--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files"]:
        assert flag in args
    assert "--continue" not in args and "--resume" not in args
    with pytest.raises(ValueError):
        launch_arguments("pi", tmp_path, "latest")


def test_correlated_response_and_settled_turn():
    async def run():
        client = await fake()
        try:
            await client.command("set_auto_retry", enabled=False)
            result = await client.prompt("hello")
            assert result["leafId"] == "entry_1"
            assert client.process.returncode is None
            with pytest.raises(PiProtocolError):
                await client.command("bash", command="echo forbidden")
            with pytest.raises(PiProtocolError):
                await client.command("set_auto_retry", enabled=True)
            await client.abort()
        finally:
            await client.close()
        assert client.process.returncode is not None
    asyncio.run(run())


def test_agent_end_without_settled_times_out_and_terminates():
    async def run():
        client = await fake()
        with pytest.raises(asyncio.TimeoutError):
            await client.prompt("hang", timeout=0.15)
        assert client.process.returncode is not None
    asyncio.run(run())


@pytest.mark.parametrize("expression", [repr("not json\n"), repr("[]\n"), f"'x' * {PiRPC.MAX_FRAME + 1} + '\\n'"],
                         ids=["invalid-json", "not-object", "oversized"])
def test_bad_frames_fail_closed(expression):
    async def run():
        # Input comes from a synthetic fixture, not a user-supplied program.
        code = "import sys; sys.stdin.readline(); sys.stdout.write(" + expression + "); sys.stdout.flush()"
        client = await fake(code)
        try:
            with pytest.raises(PiProtocolError):
                await client.command("get_state")
        finally:
            await client.close()
    asyncio.run(run())


def test_unicode_separators_are_not_jsonl_delimiters():
    async def run():
        code = "import sys,json; r=json.loads(sys.stdin.readline()); print(json.dumps(dict(type='response',id=r['id'],command='get_state',success=True,data={'text':'a\\u2028b\\u2029c'}),ensure_ascii=False),flush=True)"
        client = await fake(code)
        try:
            result = await client.command("get_state")
            assert result["text"] == "a\u2028b\u2029c"
        finally:
            await client.close()
    asyncio.run(run())
