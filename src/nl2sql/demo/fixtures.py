"""EXPLICIT demo fixtures.  Synthetic values only - never business data.

Every fixture here is deterministic and clearly DEMO-marked, so a demo result
can never be mistaken for a production business value.  No customer data and no
copied production metric values are present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.nl2sql.contracts import ScopeLevel

# Explicit demo identities.  The provenance strings are deliberately distinct
# from any production datasource/role so a demo receipt cannot claim to be one.
DEMO_DATASOURCE: Final[str] = "demo_synthetic_datasource"
DEMO_READONLY_ROLE: Final[str] = "demo_synthetic_readonly_role"
DEMO_SOURCE_ID: Final[str] = "demo_synthetic_source"


@dataclass(frozen=True, slots=True)
class DemoMetricValue:
    """One deterministic synthetic authoritative metric value."""

    metric_key: str
    display_name: str
    value: str
    unit: str
    # Watermark the demo result is "as of"; fixed so runs are reproducible.
    data_as_of: str = "2026-01-01"
    # An analysis-style concept is deliberately NOT served by QUERY.
    retrieval_only: bool = True


DEMO_METRICS: Final[tuple[DemoMetricValue, ...]] = (
    DemoMetricValue(
        metric_key="demo.revenue",
        display_name="Demo revenue",
        value="1250000",
        unit="currency_cny",
    ),
    DemoMetricValue(
        metric_key="demo.cost",
        display_name="Demo cost",
        value="780000",
        unit="currency_cny",
    ),
)

DEMO_METRICS_BY_KEY: Final[dict[str, DemoMetricValue]] = {
    metric.metric_key: metric for metric in DEMO_METRICS
}

# Words the bounded demo resolver understands for authoritative retrieval.
DEMO_RETRIEVAL_TERMS: Final[dict[str, str]] = {
    "revenue": "demo.revenue",
    "demo revenue": "demo.revenue",
    "cost": "demo.cost",
    "demo cost": "demo.cost",
}

# A deliberately UNSUPPORTED concept: QUERY must return cannot_resolve and
# suggest ANALYZE rather than invoking a model.
DEMO_UNSUPPORTED_TERMS: Final[tuple[str, ...]] = (
    "why",
    "analyse",
    "analyze",
    "explain",
    "forecast",
    "predict",
    "trend",
)

# A term that needs a BOUNDED CLARIFICATION (a resolvable slot), so the
# clarification path stays a clarification and is never a mode suggestion.
DEMO_AMBIGUOUS_TERM: Final[str] = "margin"

# DEMO-ONLY synthetic semantic candidate ids.  They exist ONLY so an
# incomplete/ambiguous ContextBundle stays a VALID contract (asset_ids requires
# at least one entry).  They are NOT executable metrics, they grant no
# authority, and they are never served by the demo metric runner.
DEMO_UNSUPPORTED_ASSET_ID: Final[str] = "demo.unsupported"
DEMO_AMBIGUOUS_ASSET_ID: Final[str] = "demo.ambiguous.margin"


@dataclass(frozen=True, slots=True)
class DemoPublishedMetric:
    """A demo published metric identity with immutable versions."""

    identity_id: str
    display_name: str
    owner_label: str = "demo publisher"


@dataclass(frozen=True, slots=True)
class DemoPublishedVersion:
    identity_id: str
    version: int
    value: str
    unit: str
    certification_state: str = "uncertified"
    withdrawn: bool = False


DEMO_PUBLISHED_IDENTITY: Final[DemoPublishedMetric] = DemoPublishedMetric(
    identity_id="demo.metric.margin",
    display_name="Demo margin",
)

# The EXPLICIT current-version pointer per identity.  This is a fixture fact,
# deliberately NOT inferred from a numeric maximum: a version can exist in the
# catalogue without being current (e.g. a staged or withdrawn future version).
DEMO_PUBLISHED_CURRENT_VERSION: Final[dict[str, int]] = {
    "demo.metric.margin": 2,
}


DEMO_PUBLISHED_VERSIONS: Final[tuple[DemoPublishedVersion, ...]] = (
    DemoPublishedVersion(
        identity_id="demo.metric.margin",
        version=1,
        value="0.37",
        unit="ratio",
        certification_state="certified",
    ),
    DemoPublishedVersion(
        identity_id="demo.metric.margin",
        version=2,
        value="0.41",
        unit="ratio",
    ),
)


@dataclass(frozen=True, slots=True)
class DemoIdentityFixture:
    """A demo scope fixture for the synthetic authorization provider."""

    user_id: str
    scope_level: ScopeLevel
    allowed_scope_ids: tuple[str, ...]


DEMO_SCOPE_FIXTURES: Final[tuple[DemoIdentityFixture, ...]] = (
    DemoIdentityFixture(
        user_id="demo-analyst",
        scope_level="team",
        allowed_scope_ids=("demo-team-1",),
    ),
    DemoIdentityFixture(
        user_id="demo-manager",
        scope_level="area",
        allowed_scope_ids=("demo-area-1", "demo-area-2"),
    ),
)
