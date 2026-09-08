import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from research_arms.session_fork import fork_completed_session, verify_completed_fork


@pytest.mark.skipif(not os.environ.get("ARMS_TEST_PI"), reason="Optional installed Pi SDK test")
@pytest.mark.parametrize("labels", [False, True])
def test_native_exact_completed_entry_excludes_future_history(tmp_path, labels):
    sdk = Path(os.environ["ARMS_TEST_PI"]).resolve().parent / "index.js"
    node = Path("/opt/homebrew/bin/node")
    script = r'''
      const {SessionManager} = await import(process.argv[1]);
      const m = SessionManager.create(process.argv[2], process.argv[2]);
      m.appendMessage({role:"user",content:"First question",timestamp:1});
      const target = m.appendMessage({role:"assistant",content:[{type:"text",text:"Completed answer"}],stopReason:"stop",timestamp:2});
      if (process.argv[3] === "true") m.appendLabelChange(target, "Selected result");
      m.appendMessage({role:"user",content:"LATER_PRIVATE_CANARY",timestamp:3});
      m.appendMessage({role:"assistant",content:[{type:"text",text:"Later answer"}],stopReason:"stop",timestamp:4});
      console.log(JSON.stringify({source:m.getSessionFile(),session_id:m.getSessionId(),entry_id:target}));
    '''
    result = subprocess.run([str(node), "--input-type=module", "-e", script, sdk.as_uri(), str(tmp_path / "source"), str(labels).lower()],
                            check=True, capture_output=True, text=True, timeout=20, env={})
    original = json.loads(result.stdout)
    source = Path(original["source"])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    async def run():
        fork = await fork_completed_session(node=node, sdk_module=sdk,
            destination=tmp_path / "fork", **original)
        content = Path(fork["path"]).read_text()
        assert "Completed answer" in content
        assert "LATER_PRIVATE_CANARY" not in content and "Later answer" not in content
        assert fork["session_id"] != original["session_id"]
        assert fork["retained_entries"] == 2
        verified = verify_completed_fork(candidate=fork["path"], **original)
        assert verified["session_id"] == fork["session_id"]
        changed = tmp_path / "tampered.jsonl"
        changed.write_text(content.replace("Completed answer", "Invented answer"))
        with pytest.raises(ValueError, match="content differs"):
            verify_completed_fork(candidate=changed, **original)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        with pytest.raises(ValueError):
            await fork_completed_session(node=node, sdk_module=sdk,
                destination=tmp_path / "bad", **{**original, "entry_id": "missing"})
        assert not list((tmp_path / "bad").glob("*.jsonl"))
    asyncio.run(run())
