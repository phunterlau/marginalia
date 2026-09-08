"""Durable post-ingestion shared paper thread work, independent of paid jobs."""
from .registry import now


class PaperThreadWorker:
    def __init__(self, registry, access, submissions, threads):
        self.registry, self.access = registry, access
        self.submissions, self.threads = submissions, threads
        self.stopping = False

    async def work_once(self):
        if self.stopping: return None
        with self.registry.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM paper_thread_jobs WHERE state='RUNNING'").fetchone(): return None
            row = db.execute("SELECT s.* FROM paper_thread_jobs j JOIN paper_submissions s ON s.id=j.submission_id WHERE j.state='QUEUED' ORDER BY j.created_at,j.submission_id LIMIT 1").fetchone()
            if row is None: return None
            row = dict(row)
            db.execute("UPDATE paper_thread_jobs SET state='RUNNING' WHERE submission_id=?", (row["id"],))
            db.execute("INSERT INTO events(kind,subject,at) VALUES ('thread_job_dispatched',?,?)", (row["id"], now()))
        try:
            scope = self.submissions._scope(row)
            destination = await self.access.authorize(row["actor"], channel_id=row["channel_id"],
                guild_id=row["guild_id"], expected_space=scope.writable_space)
            parent = destination["parent_channel_id"] or row["channel_id"]
            if self.stopping: raise RuntimeError("Thread worker stopping")
            self.submissions._scope(row)
            result = await self.threads.ensure(row["actor"], row["guild_id"], parent,
                scope.writable_space, row["job_id"])
            self.submissions._scope(row)
            with self.registry.connect() as db:
                db.execute("UPDATE paper_thread_jobs SET state='COMPLETE',thread_id=? WHERE submission_id=?",
                    (result["thread_id"], row["id"]))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('thread_job_completed',?,?)", (row["id"], now()))
            return row["id"]
        except BaseException:
            with self.registry.connect() as db:
                db.execute("UPDATE paper_thread_jobs SET state='NEEDS_ATTENTION' WHERE submission_id=?", (row["id"],))
                db.execute("INSERT INTO events(kind,subject,at) VALUES ('thread_job_uncertain',?,?)", (row["id"], now()))
            raise
