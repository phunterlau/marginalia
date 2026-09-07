# Local backup, restore and migration

Maintenance is trusted local administration. It does not call models, accept
research cards, publish data, or install a background service.

```sh
research --space personal doctor
research --space personal backup create /absolute/backups/snapshot-001
research backup verify /absolute/backups/snapshot-001
research backup restore /absolute/backups/snapshot-001 --to /absolute/new-restored-root

# Explicit migration (also enables WAL on compatible older DELETE-mode stores):
research --root /absolute/new-restored-root migrate --backup /absolute/backups/before-migration
research --root /absolute/new-restored-root doctor
```

`doctor` checks SQLite integrity and foreign keys, schema compatibility, asset
checksums, job-state counts, and unreferenced files. It reports orphans and staging
files without deleting them. An unhealthy report returns exit code 1; operational
errors return 2. Doctor reads the existing database without initializing it.

## Snapshot contract

A snapshot includes `brain.sqlite3`, `jobs.sqlite3` if present, and all referenced
source assets. The local space registry, Pi sessions, orphan assets and other
runtime artifacts are **not** included. Back those up separately according to
their original access scope. Never treat a restored corpus as shared implicitly.

Backup refuses an active absorption worker. It takes the job-database writer lock
before the canonical writer lock, copies each database through SQLite's backup
API (including committed WAL content), copies immutable referenced assets, and
verifies checksums and database integrity. A manifest maps source-asset IDs to
content-addressed snapshot members. The complete snapshot is published only
after verification; failed unpublished staging directories are retained for
inspection. Backup destinations must be new and outside the corpus root.

These checks detect corruption; a manifest is not a signed authenticity proof.
Restore only trusted local snapshots. Path traversal, symlink members, inventory
mismatches and unexpected WAL content are rejected.

## Restore policy

Restore requires a new destination; it never overwrites existing data. Asset
paths are rewritten to that root while paper, compilation, block, card, evidence,
review and event identities remain unchanged. Review state is preserved.

Queued or running jobs become `NEEDS_ATTENTION` with an audit event. A copied
approval must never silently restart provider spending. Inspect uncertain calls,
then explicitly acknowledge/retry and approve if work should continue. Job
database space identity is retained; registering under an unrelated space name
does not bypass that identity check.

The original corpus and snapshot remain unchanged. Open the restored root
directly for verification before deliberately updating any space registration.
Old-schema snapshots can be restored for explicit migration; restore integrity
verification is not a claim of current schema compatibility.

## Migration policy

Opening an existing Brain no longer implicitly migrates it, even through the
legacy Python constructor or `--root` CLI. Fresh roots still initialize normally.
Incompatible schemas fail closed with a migration instruction.

`migrate --backup` first creates and verifies a complete snapshot, while excluding
the absorption worker. Pending DDL and migration-version records are then applied
in one SQLite transaction; failure rolls them back together. Unknown or
non-contiguous migration histories are refused. Do not run separate trusted
administrative writers during maintenance. A failed migration leaves its verified
pre-migration snapshot available for restore to a new directory.

## Verification

The synthetic suite covers active-worker exclusion, source corruption, failed
copies, restore quarantine, asset relocation, orphan preservation, migration
rollback, and malicious/inconsistent snapshot member inventories. A local
eight-paper corpus has also been snapshot/restored and compared for row counts,
origin/review counts, and exact top-five reliable-recall identities. Full archives
and restore-validation data remain local rather than committed examples.
