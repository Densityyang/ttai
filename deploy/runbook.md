# Release and recovery runbook

This runbook assumes a deployment host with Docker Compose, independently
persisted control/checkpoint PostgreSQL services, and the secret files described
by the Compose release profile. The business PostgreSQL service remains external
and read-only. The application image is always addressed by a digest; `latest`
and mutable tags are not valid rollback inputs.

## Preflight and release order

1. Verify `DEPLOY_ENVIRONMENT=staging` or `production`, the `ttai` Compose
   project name, a host secret directory with mode `0700` and read-only files
   (mode `0444` for non-root Compose services), disk watermarks, and the release
   manifest checksum. Owner, app, migrator and backup roles must use different
   secret files.
2. Run the fake-provider regression and model smoke gate. Store only the
   redacted report and its run ID in the manifest.
3. Set an absolute `BACKUP_HOST_DIR` on storage outside both PostgreSQL named
   volumes, with mode `0700` and enough capacity for control and checkpoint
   dumps. Only the capability-dropped one-shot migration/restore jobs may read it.
4. Run `scripts/deploy.sh deploy/release-<id>.yaml` with
   `TTAI_IMAGE_REPOSITORY` set to the registry/repository. The script applies
   the following guarded order: start both state databases, apply the idempotent
   role bootstrap (including upgrades from existing named volumes), back up both
   databases, verify the checkpoint snapshot checksum, run expand-only Alembic and LangGraph
   migrations, optionally build the semantic candidate when
   `RUN_SEMANTIC_INDEXER=1`, update both API instances, then reload Nginx and run
   smoke checks through the public Nginx port.
5. Observe readiness, error rate, audit-outbox age, database connections, disk
   watermarks, and model cost for 30 minutes before increasing traffic.

## Rollback

Stop traffic expansion immediately for data leakage, dangerous-SQL regression,
outbox backlog, restore failure, or route SLO breach. Select the previous
manifest and run:

```bash
DEPLOY_ENVIRONMENT=production \
TTAI_IMAGE_REPOSITORY=registry.example/ttai \
CURRENT_RELEASE_MANIFEST=deploy/release-<current-id>.yaml \
scripts/rollback.sh deploy/release-<previous-id>.yaml --confirm-rollback
```

The rollback script requires an explicit confirmation flag, verifies the
manifest, restores the previous image digest, moves the semantic active pointer
inside control PostgreSQL, updates both API instances, reloads Nginx, and runs
the smoke checks. It never invokes a legacy engine or a mutable image tag.

## Backup and restore rehearsal

`scripts/backup.sh` writes timestamped custom-format dumps to
`BACKUP_HOST_DIR/control` and `BACKUP_HOST_DIR/checkpoint`, verifies each archive,
and atomically updates `latest.dump`, `latest.dump.sha256`, and
`latest.manifest` in each directory. Retention defaults to 7 days. The backup
directory must be on a separate disk or object-storage sync target, not under a
PostgreSQL volume or the image source tree.

Run a rehearsal against databases whose names end in `_restore_test`; the
restore script rejects every other target name:

```bash
DEPLOY_ENVIRONMENT=staging \
BACKUP_HOST_DIR=/srv/ttai-backups \
scripts/restore-test.sh --confirm-restore
```

Record both dump checksums, both schema revisions, control audit/outbox probes,
and a thread/HITL resume check. External business PostgreSQL is owned by its data
custodian; this project never migrates, restores, or writes to it and only runs
approved read-only compatibility and performance checks.

## Incident operations

- Disk >80%: stop benchmark expansion, prune only expired dumps according to
  retention, and verify the latest checksum before deleting anything.
- Disk >90% or outbox age >15 minutes: stop release traffic and page the
  operator; do not run `down -v`.
- PostgreSQL restore failure: keep the active release pointer unchanged, isolate
  the restore target, and escalate to the data custodian.
- Certificate rotation: install the new certificate with restrictive
  permissions, validate Nginx configuration, reload Nginx, then revoke the old
  certificate after a successful smoke check. Keys never enter Compose env or
  application trace state.
