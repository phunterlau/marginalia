"""Bounded Pi JSONL client. No shell, default tools, implicit plugins, or retries.

This low-level client is not a user API. A supervisor must inject scoped tools,
authorize before prompting, ledger dispatch and quarantine interrupted work.
"""
import asyncio
import json
import os
from pathlib import Path
import uuid


class PiProtocolError(RuntimeError):
    pass


def launch_arguments(executable, session_directory, session_id):
    executable, directory = Path(executable), Path(session_directory)
    if not executable.is_absolute() or not directory.is_absolute():
        raise ValueError("Pi executable and session directory must be absolute")
    if str(uuid.UUID(session_id)) != session_id:
        raise ValueError("Exact Pi session UUID required")
    return [str(executable), "--mode", "rpc", "--session-dir", str(directory),
            "--session-id", session_id, "--no-tools", "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-context-files",
            "--system-prompt", "You are a read-only research assistant. Distinguish source context, unreviewed interpretations and accepted memory. Do not claim tool access you do not have."]


class PiRPC:
    ALLOWED = {"get_state", "get_entries", "get_session_stats", "get_last_assistant_text",
               "set_auto_retry", "set_auto_compaction", "prompt", "clear_queue", "abort"}
    MAX_FRAME = 1_000_000

    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.events = asyncio.Queue(maxsize=256)
        self.failure = None
        self.stderr_bytes = 0
        self.tools_ready = asyncio.Event()
        self.prompt_lock = asyncio.Lock()
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def start(cls, executable, session_directory, session_id, *, agent_directory=None, tool_auth_file=None):
        argv = launch_arguments(executable, session_directory, session_id)
        if tool_auth_file is not None:
            auth_path = Path(tool_auth_file).resolve(strict=True)
            argv[argv.index("--no-tools")] = "--no-builtin-tools"
            argv.extend(["--tools", "research_recall,research_evidence,research_object,research_discussed", "--extension",
                         str(Path(__file__).parent / "integrations" / "research-tools.ts")])
        directory = Path(session_directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not pass API keys or unrelated environment secrets to Pi. Its
        # configured login may still be used through the normal auth store.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "SHELL", "TMPDIR", "LANG", "TERM") if key in os.environ}
        if agent_directory is not None:
            env["PI_CODING_AGENT_DIR"] = str(Path(agent_directory).resolve())
        if tool_auth_file is not None:
            env["ARMS_TOOL_AUTH_FILE"] = str(auth_path)
        process = await asyncio.create_subprocess_exec(*argv, cwd=directory, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=cls.MAX_FRAME + 1)
        client = cls(process)
        try:
            await client.command("set_auto_retry", enabled=False)
            await client.command("set_auto_compaction", enabled=True)
            state = await client.command("get_state")
            if state.get("sessionId") != session_id or state.get("isStreaming") or state.get("pendingMessageCount", 0):
                raise PiProtocolError("Pi session mismatch or unexpectedly active startup")
            if tool_auth_file is not None:
                await asyncio.wait_for(client.tools_ready.wait(), 20)
                if client.failure:
                    raise client.failure
            return client
        except BaseException:
            await client.close()
            raise

    async def _drain_stderr(self):
        # Drain so errors cannot deadlock the subprocess; do not propagate raw
        # stderr (which can contain paths or authentication details) to users.
        while chunk := await self.process.stderr.read(4096):
            self.stderr_bytes += len(chunk)

    def _fail(self, message):
        self.failure = PiProtocolError(message)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        try:
            self.events.put_nowait({"type": "protocol_failure"})
        except asyncio.QueueFull:
            pass

    async def _read(self):
        try:
            while True:
                frame = await self.process.stdout.readline()  # LF only, not Unicode splitlines.
                if not frame:
                    raise PiProtocolError("Pi process output closed")
                if len(frame) > self.MAX_FRAME or not frame.endswith(b"\n"):
                    raise PiProtocolError("Invalid or oversized Pi frame")
                event = json.loads(frame)
                if not isinstance(event, dict):
                    raise PiProtocolError("Pi frame must be an object")
                if event.get("type") == "extension_ui_request" and event.get("method") == "notify":
                    notice = json.loads(event.get("message", "{}"))
                    if notice.get("type") != "arms_tools_ready":
                        raise PiProtocolError("Unexpected Pi notification")
                    if sorted(notice.get("tools", [])) != ["research_discussed", "research_evidence", "research_object", "research_recall"]:
                        raise PiProtocolError("Unexpected active Pi tools")
                    self.tools_ready.set()
                elif event.get("type") == "response":
                    future = self.pending.get(event.get("id"))
                    if future is not None and not future.done():
                        future.set_result(event)
                elif event.get("type") in {"agent_end", "agent_settled", "auto_retry_start", "extension_error"}:
                    self.events.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only code-owned classifications, never raw frames or provider errors.
            import logging
            logging.getLogger(__name__).warning("Pi reader failed: %s", type(exc).__name__)
            self._fail("Pi protocol interrupted; inspect local session before retry")

    async def command(self, name, *, timeout=20, **data):
        if name not in self.ALLOWED or "id" in data or "type" in data:
            raise PiProtocolError("RPC command unavailable")
        if name == "set_auto_retry" and data != {"enabled": False}:
            raise PiProtocolError("Automatic retries are disabled")
        if self.failure:
            raise self.failure
        request_id = uuid.uuid4().hex
        payload = json.dumps({"id": request_id, "type": name, **data}, ensure_ascii=False).encode() + b"\n"
        if len(payload) > self.MAX_FRAME:
            raise ValueError("RPC request too large")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write(payload)
            await asyncio.wait_for(self.process.stdin.drain(), timeout)
            result = await asyncio.wait_for(future, timeout)
            if result.get("command") != name or result.get("success") is not True:
                raise PiProtocolError("Pi rejected the request; inspect local session")
            return result.get("data") or {}
        finally:
            self.pending.pop(request_id, None)

    async def prompt(self, message, *, timeout=300, since=None):
        # Trusted supervisor envelope includes attribution and a bounded reply
        # anchor; the user question is separately limited to 20k by the registry.
        if not isinstance(message, str) or not message.strip() or len(message) > 30_000:
            raise ValueError("Prompt envelope must contain 1..30000 characters")
        async with self.prompt_lock:
            if not self.events.empty():
                raise PiProtocolError("Unconsumed Pi events; do not reuse uncertain session")
            # prompt acknowledgement and agent_end are NOT completion signals.
            async def settle():
                while True:
                    event = await self.events.get()
                    if self.failure:
                        raise self.failure
                    if event["type"] == "agent_settled":
                        break
                    if event["type"] in {"auto_retry_start", "extension_error"}:
                        import logging
                        logging.getLogger(__name__).warning("Pi turn rejected event type: %s", event["type"])
                        raise PiProtocolError("Unexpected retry or extension failure")
                state = await self.command("get_state")
                if state.get("isStreaming") or state.get("isCompacting") or state.get("pendingMessageCount", 0):
                    raise PiProtocolError("Pi did not settle")
                return await self.command("get_entries", **({"since": since} if since else {}))
            try:
                await self.command("prompt", message=message, timeout=min(timeout, 20))
                return await asyncio.wait_for(settle(), timeout)
            except BaseException:
                # Caller must ledger uncertainty. Never automatically replay.
                await self.close()
                raise

    async def abort(self):
        await self.command("clear_queue")
        await self.command("abort")

    async def close(self):
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.reader.cancel()
        self.stderr_reader.cancel()
        await asyncio.gather(self.reader, self.stderr_reader, return_exceptions=True)
        self._fail("Pi client closed")
