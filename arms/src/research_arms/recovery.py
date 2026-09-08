"""Explicit trusted local recovery; no Discord connection or provider replay."""
import argparse
from contextlib import closing
import fcntl
import json
import os
import sqlite3
import uuid

from research_brain.spaces import SpaceRegistry
from .registry import ArmsRegistry


def recover(registry, *, confirmed_stopped=False):
    """Operator confirms orphaned Pi/provider processes have also been stopped.

    The ownership lock excludes the Gateway supervisor, but cannot establish that
    all of a killed supervisor's children have terminated. Never infer that from
    a stale lock file alone.
    """
    if confirmed_stopped is not True:
        raise ValueError("Confirm previous workers and orphaned child processes are stopped")
    lock = os.open(registry.root / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A worker still owns the registry; stop it before recovery") from None
        backup = registry.root / ("arms-recovery-backup-" + uuid.uuid4().hex + ".sqlite3")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with registry.connect(readonly=True) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Recovery backup verification failed")
        result = registry.recover_stopped_workers(confirmed_stopped=True)
        return {**result, "backup": str(backup), "notice": "Uncertain work quarantined, not retried. Inspect before explicit reconciliation. Brain paid-job ledgers are unchanged."}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description="Quarantine interrupted Arms work after all prior workers are stopped")
    parser.add_argument("--root", required=True, help="Existing Arms operational directory")
    parser.add_argument("--spaces-root", required=True, help="Existing Brain space registry")
    parser.add_argument("--confirm-workers-stopped", action="store_true")
    args = parser.parse_args()
    if not args.confirm_workers_stopped:
        parser.error("Verify all prior workers and orphaned child processes have stopped, then pass --confirm-workers-stopped")
    registry = ArmsRegistry(args.root, SpaceRegistry(args.spaces_root))
    print(json.dumps(recover(registry, confirmed_stopped=True), sort_keys=True))


if __name__ == "__main__":
    main()
