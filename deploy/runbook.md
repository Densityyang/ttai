# Release and recovery runbook

This runbook assumes a deployment host with Docker Compose, an external
control/checkpoint PostgreSQL service, and the secret files described by the
Compose release profile. The application image is always addressed by a
digest; `latest` and mutable tags are not valid rollback inputs.

## Preflight and release order

1. Verify `DEPLOY_ENVIRONMENT=staging` or `production`, the `ttai` Compose
   project name, secret-file permissions (`0600`), disk watermarks, and the
   release manifest checksum.
2. Run the fake-provider regression and model smoke gate. Store only the
   redacted report and its run ID in the manifest.
3. Run `scripts/backup.sh` and confirm `latest.dump` plus its SHA-256 exists on
   storage outside the PostgreSQL named volume.
4. Run `scripts/deploy.sh deploy/release-<id>.yaml` with
   `TTAI_IMAGE_REPOSITORY` set to the registry/repository. The script applies
   migrations first, optionally builds the semantic candidate when
   `RUN_SEMANTIC_INDEXER=1`, updates both API instances, then reloads Nginx and
   runs the smoke checks through the public Nginx port.
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

`scripts/backup.sh` writes timestamped custom-format dumps and atomically updates
`latest.dump` in `BACKUP_HOST_DIR` (default `var/backups`). Retention defaults
to 30 days. The backup directory must be on a separate disk or object-storage
sync target, not under a PostgreSQL volume or the image source tree.

Run a rehearsal against an isolated restore database, never the production
database:

```bash
DEPLOY_ENVIRONMENT=staging \
BACKUP_HOST_DIR=/srv/ttai-backups \
scripts/restore-test.sh --confirm-restore
```

Record the dump checksum, schema revision, row counts for query runs and
audit/outbox, and a thread/HITL resume check. External business PostgreSQL is
owned by its data custodian; this project only verifies the approved read-only
connection and post-restore smoke query.

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
