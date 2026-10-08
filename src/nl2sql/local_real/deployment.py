"""Local real-data deployment adapter (LOCAL DEMO ONLY).

Wraps the EXISTING typed pipeline: it supplies the same (views, read_active,
read_snapshot, gateway) interface that build_request_typed_runtime consumes, but
builds the semantic release and schema snapshot IN MEMORY rather than requiring
production Control-DB publication.

The real business SQL boundary is unchanged: QueryGateway remains the only
executor, driven by the existing compiler/validator/executor.  There is NO
second SQL implementation and no hand-written case SQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from src.nl2sql.semantic.registry import SemanticRelease
from src.nl2sql.semantic.schema_snapshot import SchemaSnapshot

logger = logging.getLogger(__name__)

# The ONE frozen real-data case this lane advertises.  The dependency closure is
# listed explicitly so unrelated authoritative metrics are NOT exposed as
# demonstrated real-data coverage.
FROZEN_REAL_CASE_METRIC_KEY = "repair_service_archive_rate_overall_day"
FROZEN_REAL_CASE_DISPLAY = "报修服务归档及时率（全局-日）"
FROZEN_REAL_CASE_DEPENDENCIES: tuple[str, ...] = (
    "repair_service_archive_rate_overall_day",
    "repair_service_archive_on_time_count_overall_day",
    "repair_service_calc_total_count_overall_day",
)
# The published AI view for this case, and the physical source it is built from.
# The fallback relation is NEVER executed by this adapter: it exists so the
# infrastructure gap (view missing, source present) is REPORTED precisely.
FROZEN_REAL_CASE_VIEW_NAME = "v_repair_service"
PREFERRED_PUBLISHED_RELATION = "ai_views.v_repair_service"
FALLBACK_PHYSICAL_RELATION = "public.silver_repair_service"

# Columns the frozen closure REQUIRES in the bound live relation.  These are the
# physical fields represented by the canonical dependencies: the count identity,
# the business time, the numerator eligibility flags and the denominator
# eligibility column.
FROZEN_REAL_CASE_REQUIRED_COLUMNS: tuple[str, ...] = (
    "id",
    "report_time",
    "completion_receipt_time",
    "is_archive_on_time",
    "is_valid_for_metrics",
)

# The published AI view carries NO physical index evidence, so the frozen case
# is admitted through a BOUNDED bootstrap scan whose cap is a DEPLOYMENT fact.
# It is never derived from a request, a model, a user or the row estimate.
LOCAL_REAL_BOOTSTRAP_SCAN_MAX_ROWS = 100_000

# Relation status vocabulary for the live probe.
RELATION_STATUS_NOT_PROBED = "NOT_PROBED"
RELATION_STATUS_PUBLISHED_VIEW_READY = "PUBLISHED_VIEW_READY"
RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT = "PUBLISHED_VIEW_MISSING_SOURCE_PRESENT"
RELATION_STATUS_SOURCE_MISSING = "SOURCE_MISSING"


@dataclass(frozen=True, slots=True)
class LocalRealRelationProbe:
    """The live relation availability result, without any business row data."""

    status: str
    preferred_relation: str = PREFERRED_PUBLISHED_RELATION
    fallback_relation: str = FALLBACK_PHYSICAL_RELATION
    missing_columns: tuple[str, ...] = ()

    @property
    def view_ready(self) -> bool:
        return self.status == RELATION_STATUS_PUBLISHED_VIEW_READY


@dataclass
class LocalRealSemanticDeployment:
    """In-memory semantic deployment built from the verified authoritative sources."""

    views: Any
    # The REAL shared read-only gateway.  It is stored because the container
    # composes the typed deployment inputs from THIS record; validating the
    # argument and then dropping it silently disabled that path.
    gateway: Any
    release: SemanticRelease
    snapshot: SchemaSnapshot
    relation_name: str
    metric_keys: tuple[str, ...] = FROZEN_REAL_CASE_DEPENDENCIES
    produced_dates: dict[str, str] = field(default_factory=dict)
    latest_authoritative_date: str | None = None
    recent_authoritative_dates: tuple[str, ...] = ()
    count_column: str = "id"
    relation_probe: LocalRealRelationProbe | None = None

    async def read_active(self) -> SemanticRelease | None:
        """The in-memory ACTIVE release; no Control-DB read."""
        return self.release

    async def read_snapshot(self, snapshot_id: str) -> SchemaSnapshot | None:
        """EXACTLY the bound validated snapshot; a wrong id returns None."""
        if self.snapshot is None or self.snapshot.snapshot_id != snapshot_id:
            return None
        return self.snapshot


def build_frozen_case_closure(
    metric_keys: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """The bounded dependency closure for the frozen real case."""
    requested = set(metric_keys or FROZEN_REAL_CASE_DEPENDENCIES)
    allowed = set(FROZEN_REAL_CASE_DEPENDENCIES)
    unknown = requested - allowed
    if unknown:
        # Advertising unrelated real metrics is not supported this wave.
        raise ValueError(
            "unsupported real-data metric(s): " + ",".join(sorted(unknown))
        )
    return tuple(key for key in FROZEN_REAL_CASE_DEPENDENCIES if key in requested)


def missing_required_columns(
    available_columns: Iterable[str],
    required: Iterable[str] = FROZEN_REAL_CASE_REQUIRED_COLUMNS,
) -> tuple[str, ...]:
    """The required frozen-closure columns absent from a live relation."""

    available = {str(name).lower() for name in available_columns}
    return tuple(
        sorted(name for name in required if name.lower() not in available)
    )


def build_local_real_deployment(
    *,
    views: Any,
    gateway: Any,
    release: SemanticRelease,
    snapshot: SchemaSnapshot,
    relation_name: str,
    relation_probe: LocalRealRelationProbe | None = None,
    latest_authoritative_date: str | None = None,
    recent_authoritative_dates: tuple[str, ...] = (),
    produced_dates: dict[str, str] | None = None,
    count_column: str = "id",
) -> LocalRealSemanticDeployment:
    """Compose the local-real deployment around the EXISTING gateway."""
    missing = [
        name
        for name, value in (
            ("views", views),
            ("gateway", gateway),
            ("release", release),
            ("snapshot", snapshot),
        )
        if value is None
    ]
    if missing:
        raise ValueError("local-real deployment missing: " + ",".join(missing))
    return LocalRealSemanticDeployment(
        views=views,
        gateway=gateway,
        release=release,
        snapshot=snapshot,
        relation_name=relation_name,
        produced_dates=dict(produced_dates or {}),
        latest_authoritative_date=latest_authoritative_date,
        recent_authoritative_dates=recent_authoritative_dates,
        count_column=count_column,
        relation_probe=relation_probe,
    )


__all__ = [
    "FALLBACK_PHYSICAL_RELATION",
    "FROZEN_REAL_CASE_DEPENDENCIES",
    "FROZEN_REAL_CASE_DISPLAY",
    "FROZEN_REAL_CASE_METRIC_KEY",
    "FROZEN_REAL_CASE_REQUIRED_COLUMNS",
    "FROZEN_REAL_CASE_VIEW_NAME",
    "LOCAL_REAL_BOOTSTRAP_SCAN_MAX_ROWS",
    "LocalRealRelationProbe",
    "LocalRealSemanticDeployment",
    "PREFERRED_PUBLISHED_RELATION",
    "RELATION_STATUS_NOT_PROBED",
    "RELATION_STATUS_PUBLISHED_VIEW_READY",
    "RELATION_STATUS_SOURCE_MISSING",
    "RELATION_STATUS_VIEW_MISSING_SOURCE_PRESENT",
    "build_frozen_case_closure",
    "build_local_real_deployment",
    "missing_required_columns",
]
