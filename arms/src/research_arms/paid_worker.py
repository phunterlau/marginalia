"""Opt-in worker for exactly approved Gateway-origin absorption jobs."""
import asyncio
import json

from .absorption import ScopedAbsorption
from .registry import Unavailable, now


class PaidAbsorptionWorker:
    def __init__(self, registry, access, *, enabled=False, service_factory=ScopedAbsorption):
        self.registry, self.access, self.enabled = registry, access, enabled
        self.service_factory = service_factory
        self.lock = asyncio.Lock()
        self.stopping = False
        self.cursor = ("", "")

    async def work_once(self, **provider_factories):
        if not self.enabled or self.stopping: return None
        async with self.lock:
            with self.registry.connect(readonly=True) as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM paper_submissions WHERE state='SOURCE_READY' AND job_id IS NOT NULL AND (created_at,id)>(?,?) ORDER BY created_at,id LIMIT 100", self.cursor)]
            if not rows:
                self.cursor = ("", "")
            for row in rows:
                self.cursor = (row["created_at"], row["id"])
                space = json.loads(row["scope_json"])["writable_space"]
                try:
                    service = self.service_factory(self.registry, row["actor"], space)
                    job = service.jobs.show(row["job_id"])
                    if job["status"] == "WAITING_APPROVAL": continue
                    if job["status"] != "QUEUED":
                        if job["status"] not in {"RUNNING", "IDLE"}:
                            self._finish(row["id"], job["status"])
                        continue
                    if job["plan"].get("authorization_context") is None: raise Unavailable()
                    approvals = [event for event in job["events"] if event["kind"] == "approved"]
                    if not approvals or not approvals[-1]["actor"].startswith("discord:"):
                        raise Unavailable()
                    approver = approvals[-1]["actor"].removeprefix("discord:")
                    loop = asyncio.get_running_loop()
                    async def discord_check():
                        if self.stopping: raise Unavailable()
                        for actor in {row["actor"], approver}:
                            # Maintainer access and actual channel visibility are
                            # independent checks, both required for every dispatch.
                            self.service_factory(self.registry, actor, space)
                            await self.access.authorize(actor, channel_id=row["channel_id"],
                                guild_id=row["guild_id"], expected_space=space)
                    await discord_check()
                    def check(context):
                        service._check(context)
                        future = asyncio.run_coroutine_threadsafe(discord_check(), loop)
                        try: future.result(timeout=30)
                        except BaseException:
                            future.cancel()
                            raise
                    service.jobs.authorization_check = check
                    result = await asyncio.to_thread(service.jobs.work_once,
                        target_job_id=row["job_id"], **provider_factories)
                    self._finish(row["id"], result["status"])
                    return result
                except Exception:
                    self._finish(row["id"], "NEEDS_ATTENTION")
            return None

    def _finish(self, ident, status):
        state = "ABSORPTION_DONE" if status == "COMPLETE" else "NEEDS_ATTENTION"
        with self.registry.connect() as db:
            db.execute("UPDATE paper_submissions SET state=? WHERE id=?", (state, ident))
            db.execute("INSERT INTO events(kind,subject,at) VALUES (?, ?, ?)",
                ("absorption_finished" if status == "COMPLETE" else "absorption_attention", ident, now()))
