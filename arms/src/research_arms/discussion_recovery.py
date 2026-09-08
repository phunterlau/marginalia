"""Trusted local inspection/retry of local discussion projections only."""
import argparse
import json

from research_brain.spaces import ContextScope, SpaceRegistry
from .registry import ArmsRegistry, encode, now


def retry(registry, job_id, *, space_id):
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 150:
        raise ValueError("Invalid discussion job ID")
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM discussion_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None: raise ValueError("Discussion job unavailable")
        data = json.loads(row["scope_json"])
        if data["writable_space"] != space_id: raise ValueError("Discussion job unavailable")
        latest = db.execute("SELECT id FROM discussion_jobs WHERE turn_id=? ORDER BY CAST(json_extract(payload_json,'$.revision') AS INTEGER) DESC LIMIT 1", (row["turn_id"],)).fetchone()[0]
        if latest != job_id: raise ValueError("Superseded discussion revision; inspect the latest job")
        if row["state"] != "NEEDS_ATTENTION": raise ValueError("Only NEEDS_ATTENTION jobs can be retried")
        payload = json.loads(row["payload_json"])
        retract = payload["deleted"] or payload.get("question_unavailable") or payload.get("answer_unavailable")
        if not retract:
            data["read_spaces"] = tuple(data["read_spaces"])
            registry.spaces.validate(ContextScope(**data))
        # The worker rechecks current Discord and Brain access before projection.
        # Privacy retractions may still remove records from their original space.
        db.execute("UPDATE discussion_jobs SET state='QUEUED' WHERE id=?", (job_id,))
        db.execute("INSERT INTO events(kind,subject,at) VALUES ('discussion_retry_requested',?,?)",
            (encode({"job_id": job_id, "space_id": space_id, "actor": "local_operator"}), now()))
        return {"job_id": job_id, "space_id": space_id, "state": "QUEUED"}


def inspect(registry, *, space_id, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100: raise ValueError("Limit must be 1..100")
    with registry.connect(readonly=True) as db:
        rows = db.execute("SELECT id,turn_id,state,created_at FROM discussion_jobs WHERE json_extract(scope_json,'$.writable_space')=? AND state='NEEDS_ATTENTION' ORDER BY created_at,id LIMIT ?", (space_id, limit + 1)).fetchall()
    return {"space_id": space_id, "items": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--spaces-root", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--retry", metavar="JOB_ID")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    registry = ArmsRegistry(args.root, SpaceRegistry(args.spaces_root))
    result = retry(registry, args.retry, space_id=args.space) if args.retry else inspect(registry, space_id=args.space, limit=args.limit)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__": main()
