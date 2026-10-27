#!/usr/bin/env python3
"""Publish the bounded executable semantic release to the Control DB.

One-shot deployment tooling for the trusted typed pipeline.  It materializes a
deployment metric binding (a ``config/metrics/*.yaml`` catalog) against the
LIVE published ai_views relations, attaches the relation policy (organization
coverage / sensitivity), validates everything fail-closed, then persists and
ACTIVATES the release through the same Control-DB boundary the runtime reads
(``ControlSemanticReleasePublisher.read_active``).

Nothing here re-authors business semantics: the catalog is the deployment
binding transcribed from the canonical Gold definition; this script only
activates it for this deployment (owner + release_status + freshness SLA),
exactly like the local-real lane, but persisted in the Control DB instead of
in-memory.

Run inside a container attached to the deployment networks, mirroring the
api service environment (DSN files, NL2SQL_DB_SCHEMA), e.g.:

    docker run --rm --network container:ttai-api-a \
      -v "$PWD":/app -w /app \
      -e CONTROL_DATABASE_URL_FILE=/run/secrets/control_app_database_url \
      -e DATABASE_URL_FILE=/run/secrets/business_ro_database_url \
      -v "$PWD/secrets/database/control_app_database_url":/run/secrets/control_app_database_url:ro \
      -v "$PWD/secrets/database/business_ro_database_url":/run/secrets/business_ro_database_url:ro \
      <ttai-image> python scripts/publish_semantic_release.py \
        --metrics-yaml config/metrics/repair_service_local_real.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import NoReturn


def _die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--metrics-yaml",
        required=True,
        help="deployment metric binding YAML (config/metrics/*.yaml)",
    )
    parser.add_argument(
        "--owner",
        default="data-platform",
        help="deployment owner stamped on the activated metrics",
    )
    parser.add_argument(
        "--freshness-sla-seconds",
        type=int,
        default=86_400,
        help="freshness SLA stamped on the activated metrics",
    )
    parser.add_argument(
        "--change-summary",
        default=None,
        help="release change summary (default: derived from the metrics yaml)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    from src.core.database import DatabasePurpose, create_runtime_async_engine
    from src.core.settings import ROOT_DIR, get_settings
    from src.nl2sql.config.settings import get_agent_config
    from src.nl2sql.semantic.authoring import validate_authoring_ir
    from src.nl2sql.semantic.materialization import materialize_authoring_ir
    from src.nl2sql.semantic.metric_contract import (
        MetricCatalog,
        load_metric_catalog,
        metric_catalog_ir,
    )
    from src.nl2sql.semantic.registry import ControlSemanticReleasePublisher
    from src.nl2sql.semantic.schema_snapshot import (
        OrganizationCoverageBinding,
        PostgresSchemaSnapshotCollector,
        RelationPolicy,
        SchemaRequirement,
        ControlSchemaSnapshotStore,
        bind_schema_snapshot,
        validate_schema_snapshot,
    )

    settings = get_settings()
    if not settings.control_database_url:
        _die("control_database_url is unavailable")
    if not settings.database_url:
        _die("database_url is unavailable")
    agent_config = get_agent_config()
    schema = agent_config.nl2sql_db_schema
    if not schema:
        _die("nl2sql_db_schema is empty")

    metrics_path = Path(args.metrics_yaml)
    if not metrics_path.is_absolute():
        metrics_path = ROOT_DIR / metrics_path
    try:
        content = metrics_path.read_text(encoding="utf-8")
    except OSError as exc:
        _die(f"metrics yaml is unreadable: {exc}")

    catalog = load_metric_catalog(content)
    if not catalog.metrics:
        _die("metrics yaml contains no metrics")

    # The deployment activates the contracts; the semantics stay the
    # Backend/Gold-authored ones carried by the binding itself.
    activated = tuple(
        type(metric).model_validate(
            {
                **metric.model_dump(mode="json"),
                "release_status": "active",
                "owner": args.owner,
                "freshness_sla_seconds": args.freshness_sla_seconds,
            }
        )
        for metric in catalog.metrics
    )
    catalog = MetricCatalog(metrics=activated)

    source_refs = sorted({metric.source_ref for metric in catalog.metrics})
    relations = {ref: f"{schema}.{ref}" for ref in source_refs}
    ir = metric_catalog_ir(catalog, relations=relations)

    # Organization coverage mirrors what the business views can physically
    # enforce: the deployment root (city_company, no id column), plus area and
    # team where the approved views carry the physical id columns.
    policies = {
        qualified: RelationPolicy(
            sensitivity="internal",
            organization_coverage=(
                OrganizationCoverageBinding(scope_level="city_company"),
                OrganizationCoverageBinding(
                    scope_level="area", field="area_id", value_type="integer"
                ),
                OrganizationCoverageBinding(
                    scope_level="team", field="team_id", value_type="integer"
                ),
            ),
            freshness_sla_seconds=args.freshness_sla_seconds,
        )
        for qualified in relations.values()
    }

    # Required live columns: business time, every predicate field, the count
    # column and the organization-coverage id columns.
    required: set[str] = {"id"}
    for metric in catalog.metrics:
        required.add(metric.business_time_column)
        required.update(item.field for item in metric.predicates)
        if metric.ratio is not None:
            required.update(item.field for item in metric.ratio.numerator_predicates)
            required.update(item.field for item in metric.ratio.denominator_predicates)
    required.add("area_id")
    required.add("team_id")

    business_engine = create_runtime_async_engine(
        settings.database_url,
        purpose=DatabasePurpose.BUSINESS_READ_ONLY,
        application_name="ttai-semantic-publish",
        settings=settings,
    )
    control_publisher: ControlSemanticReleasePublisher | None = None
    snapshot_store: ControlSchemaSnapshotStore | None = None
    try:
        snapshot_candidate = await PostgresSchemaSnapshotCollector(
            business_engine, max_relations=max(len(relations), 1)
        ).collect(
            source_identifier="production-business",
            approved_schemas=(schema,),
            approved_relations=tuple(relations.values()),
            policies=policies,
        )
        report = validate_authoring_ir(
            ir, relation_columns=snapshot_candidate.relation_columns()
        )
        if not report.ok:
            codes = sorted({issue.code for issue in report.issues})
            _die("authoring validation failed: " + ",".join(codes))
        snapshot_report = validate_schema_snapshot(
            snapshot_candidate,
            requirements=tuple(
                SchemaRequirement(qualified, tuple(sorted(required)))
                for qualified in relations.values()
            ),
        )
        if not snapshot_report.ok:
            details = "; ".join(
                f"{issue.code}@{issue.relation_id}:{issue.path} {issue.message}"
                for issue in snapshot_report.issues
            )
            _die("schema snapshot validation failed: " + details)

        snapshot_store = ControlSchemaSnapshotStore(settings.control_database_url)
        snapshot = await snapshot_store.publish(snapshot_candidate, snapshot_report)
        typed_candidate = bind_schema_snapshot(
            materialize_authoring_ir(ir, report), snapshot
        )
        control_publisher = ControlSemanticReleasePublisher(
            settings.control_database_url
        )
        change_summary = args.change_summary or (
            f"publish {metrics_path.name} ({schema})"
        )
        release = await control_publisher.publish_candidate(
            typed_candidate, change_summary=change_summary
        )
        active = await control_publisher.read_active()
    finally:
        if control_publisher is not None:
            await control_publisher.close()
        if snapshot_store is not None:
            await snapshot_store.close()
        await business_engine.dispose()

    if active is None or active.release_id != release.release_id:
        _die("release was published but is not the active release")
    print(f"active release: {release.release_id}")
    print(f"checksum: {release.checksum}")
    print(f"schema snapshot: {release.schema_snapshot_id}")
    print(f"relations: {', '.join(relations.values())}")
    print(f"metrics: {', '.join(sorted(m.metric_key for m in catalog.metrics))}")


if __name__ == "__main__":
    asyncio.run(main())
