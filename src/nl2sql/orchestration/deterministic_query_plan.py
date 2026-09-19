"""Deterministic, zero-model QueryPlan proposal for the typed QUERY path.

The provider parses a pure grammar over the question: an exact metric
selector plus closed clauses.  It makes no model call and no network call and
performs no database read of its own.  Its external inputs are the shared
``ActiveReleaseRegistry`` it is constructed with (it calls only the
synchronous ``active_release()``; the awaited control-database read belongs to
the evidence provider, not to this module) and the request-time wall clock,
sampled to resolve relative business dates in Asia/Shanghai.  The plan is
bound to the context release and fails closed when that release was never
bound or differs from ``context.semantic_release_id``.

Time is a CLOSED deterministic grammar.  Current periods are TO-DATE and
previous periods are COMPLETE calendar periods; there is NO default business
window and freshness never rewrites the requested period.  An expression the
grammar cannot resolve becomes an unresolved TIME slot; the fixed 1970 date
pair is only an inert CARRIER that the frozen QueryPlan contract requires, and
the SLOT is the authority that keeps it out of compilation and SQL -- the
carrier is not intrinsically safe and carries no business meaning.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from src.nl2sql.contracts import BoundFilter, ContextBundle, QueryPlan, RequestIdentity, TimeRange
from src.nl2sql.semantic.metric_contract import MetricContract
from src.nl2sql.semantic.metric_match import MetricCandidate, detect_metric_candidates
from src.nl2sql.semantic.policy_evidence import (
    ActiveReleaseBoundError,
    ActiveReleaseRegistry,
)

_GRAINS = ("day", "month")
_SCOPE_NAMES = ("area", "team", "employee")
# UNCONDITIONAL FAIL-CLOSED RESTRICTION: organization-scope grammar is
# UNAVAILABLE in this slice.
#
# area/team/employee is the authorization-scope channel: the grammar would emit
# it with source="entity_alias", which MetricQueryCompiler accepts at
# metric_query.py:838-849 and then interprets as a MEMBERSHIP-checked
# organization predicate whose check at :494-504 is SKIPPED whenever the
# compiler was constructed WITHOUT an authorization context.  No production
# RequestContext.authorization is populated and no container wiring supplies
# one, so emitting such a predicate is a FAIL-OPEN: an authorization-shaped
# filter nothing can check.  The grammar therefore refuses it outright.
#
# This is deliberately NOT gated on a Boolean: there is no constant to flip.
# A future slice that genuinely wires production authorization makes an explicit
# code change here, together with RequestContext.authorization population,
# Backend effective authorization, a request-scoped compiler, allowed_scope_ids
# membership enforcement, OrganizationCoverageBinding enforcement and
# authorization_revision provenance.  Nothing aspirational is claimed now.
_ORG_SCOPE_ERROR = "org_scope_requires_authorization_seam"
_INTENTS = ("metric", "trend", "comparison", "ranking")
_RANGE_DELIMITERS = ("..", "~", "\u81f3", "\u5230", ",", "\u3001")
# CLOSED alias table only: no model translation, no fuzzy matching, no
# embeddings and no open-ended semantics.  The frozen Chinese forms map to the
# same canonical expressions as the English ones.
_RELATIVE_ALIASES = {
    "\u4eca\u5929": "today",
    "\u6628\u5929": "yesterday",
    "\u672c\u5468": "this week",
    "\u4e0a\u5468": "last week",
    "\u672c\u6708": "this month",
    "\u4e0a\u6708": "last month",
    "\u4eca\u5e74": "this year",
    "\u53bb\u5e74": "last year",
}
_IN_CLAUSE = re.compile(r"^(?P<name>[a-z_]+)\s+in\s+\[(?P<values>[^\]]*)\]$")


class QueryPlanProposalError(ValueError):
    """A fail-closed proposal failure.

    Raised for malformed or unresolvable GRAMMAR input (unknown clause or
    token, unknown selector, ambiguous selector, ranking without a dimension,
    city_company for a grouped intent, an undeclared filter field, an EMPTY
    time= value), for an organization-scope request while the authorization
    seam is absent, and for a release-binding failure (no bound release, or a
    bound release whose id differs from the context release).  It is NOT raised
    for conflicting or unresolved TIME, which is clarification-possible and
    becomes a plan slot.
    """


@dataclass(frozen=True)
class UnresolvedTime:
    """A time expression the closed grammar could not resolve deterministically."""

    reason: str


UNRESOLVED_TIME = UnresolvedTime("unsupported_time_expression")


def resolve_time_expression(
    text: str,
    *,
    clock: object,
    timezone: str = "Asia/Shanghai",
) -> TimeRange | UnresolvedTime:
    """Resolve one closed time expression against an injected clock.

    ``clock`` is either a zero-argument callable returning a timezone-aware
    datetime (sampled exactly once) or that datetime itself.  The result is a
    pure function of (text, sampled instant, timezone).
    """

    instant = _sampled_instant(clock)
    if instant is None:
        return UnresolvedTime("invalid_clock")
    try:
        local = instant.astimezone(ZoneInfo(timezone))
    except (ValueError, OverflowError, OSError, KeyError):
        # An unusable timezone name (ZoneInfoNotFoundError is a KeyError) or an
        # unconvertible instant is a resolver failure, never an uncaught
        # exception on a user-reachable path.
        return UnresolvedTime("invalid_timezone")
    return _resolve_expression(text, local.date(), timezone)


def _resolve_expression(text: str, current: date, timezone: str) -> TimeRange | UnresolvedTime:
    if not isinstance(text, str):
        return UNRESOLVED_TIME
    # A whitespace-free clause value may join a multi-word period with an
    # underscore (time=this_week); the closed table itself uses spaces.  The
    # closed alias table then maps the frozen Chinese forms to the same
    # canonical expressions.
    clause = _normalize(text).replace("_", " ")
    if not clause:
        return UNRESOLVED_TIME
    period = _period_for(_RELATIVE_ALIASES.get(clause, clause), current)
    if period is None:
        return UnresolvedTime("unparseable_time_expression")
    start, end = period
    return TimeRange(start=start, end=end, timezone=timezone)


def _period_for(token: str, current: date) -> tuple[date, date] | None:
    relative = _resolve_relative(token, current)
    if relative is not None:
        return relative
    return _resolve_range(token)


def _resolve_relative(token: str, current: date) -> tuple[date, date] | None:
    """Resolve one closed relative period against the local current date.

    The WHOLE body sits inside the fail-closed boundary, so no calendar
    construction can escape from this user-reachable grammar path.  A clock at
    the calendar edge (for example year 1) makes "last year"/"last month"
    unparseable and yields None, which callers turn into UnresolvedTime.
    """

    try:
        year, month, _ = current.timetuple()[:3]
        if token == "today":
            return (current, current)
        if token == "yesterday":
            previous = current - timedelta(days=1)
            return (previous, previous)
        if token == "this week":
            return (_monday(current), current)
        if token == "last week":
            last_monday = _monday(current) - timedelta(days=7)
            return (last_monday, last_monday + timedelta(days=6))
        if token == "this month":
            first = _safe_date(year, month, 1)
            return None if first is None else (first, current)
        if token == "last month":
            first_this_month = _safe_date(year, month, 1)
            if first_this_month is None:
                return None
            last_previous = first_this_month - timedelta(days=1)
            first = _safe_date(last_previous.year, last_previous.month, 1)
            return None if first is None else (first, last_previous)
        if token == "this year":
            first = _safe_date(year, 1, 1)
            return None if first is None else (first, current)
        if token == "last year":
            start = _safe_date(year - 1, 1, 1)
            end = _safe_date(year - 1, 12, 31)
            return None if start is None or end is None else (start, end)
    except (ValueError, OverflowError):
        return None
    return None


def _resolve_range(token: str) -> tuple[date, date] | None:
    parts = _split_range(token)
    if parts is None:
        return _absolute_period(token)
    first, second = parts
    first_date = _parse_day(first)
    second_date = _parse_day(second)
    if first_date is None or second_date is None or second_date < first_date:
        return None
    return (first_date, second_date)


def _split_range(token: str) -> tuple[str, str] | None:
    for delimiter in _RANGE_DELIMITERS:
        if delimiter not in token:
            continue
        parts = token.split(delimiter)
        if len(parts) != 2:
            return None
        return parts[0].strip(), parts[1].strip()
    return None


def _absolute_period(text: str) -> tuple[date, date] | None:
    """Expand one deterministic absolute date form into an inclusive period.

    A bare day is that day, a bare month is the complete calendar month and a
    bare year is the complete calendar year.  An invalid calendar date (for
    example 2025-02-29), an out-of-range year (for example 0000) or an
    out-of-range month (for example 2025-13) is unparseable, never normalized.
    EVERY path is enclosed in the exception boundary below, so a malformed
    token can never escape as an uncaught calendar error.
    """

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = (int(value) for value in match.groups())
        single = _safe_date(year, month, day)
        return None if single is None else (single, single)
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        year, month = (int(value) for value in match.groups())
        first = _safe_date(year, month, 1)
        if first is None:
            return None
        try:
            last = _safe_date(year, month, calendar.monthrange(year, month)[1])
        except (ValueError, OverflowError):
            return None
        return None if last is None else (first, last)
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        start = _safe_date(int(match.group(1)), 1, 1)
        end = _safe_date(int(match.group(1)), 12, 31)
        return None if start is None or end is None else (start, end)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Construct a calendar date or return None; never raises.

    Validates the Python calendar bounds (year 1..9999, month 1..12) before
    construction and still wraps the call, so every calendar-reachable input
    fails closed instead of escaping as an uncaught error.
    """

    if not 1 <= year <= 9999 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _parse_day(text: str) -> date | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    year, month, day = (int(value) for value in match.groups())
    return _safe_date(year, month, day)


def _sampled_instant(clock: object) -> datetime | None:
    if isinstance(clock, datetime):
        candidate: Any = clock
    elif callable(clock):
        candidate = cast(Callable[[], Any], clock)()
    else:
        return None
    if not isinstance(candidate, datetime) or candidate.tzinfo is None:
        return None
    if candidate.utcoffset() is None:
        return None
    return candidate


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


@dataclass(frozen=True)
class ProposalIntent:
    """The deterministic selector plus its closed clauses."""

    candidate: MetricCandidate
    time_text: str | None
    time_conflict: bool
    grain: Literal["day", "month"]
    dimension: str | None
    intent: Literal["metric", "trend", "comparison", "ranking"]
    result_limit: int | None
    filters: tuple[BoundFilter, ...]


class DeterministicQueryPlanProvider:
    """Zero-model grammar parser producing one untrusted QueryPlan proposal.

    The clock is sampled ZERO-OR-ONE times per proposal: ZERO when the question
    carries no time clause and ZERO when repeated time clauses are detected as
    conflicting, otherwise EXACTLY ONCE, and every relative expression in that
    proposal resolves against that single sampled instant.

    The plan is BOUND to the context release: this provider consumes the
    already-bound release from the shared ``ActiveReleaseRegistry`` (the exact
    instance the awaited evidence phase bound) and additionally fails closed
    when its release id differs from ``context.semantic_release_id``.  It never
    re-reads the active pointer, so one request can never silently mix release
    A in the context with release B in the plan.
    """

    is_deterministic = True

    def __init__(
        self,
        registry: ActiveReleaseRegistry,
        clock: Callable[[], datetime],
    ) -> None:
        self._registry = registry
        self._clock = clock

    async def propose(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
    ) -> QueryPlan:
        del identity
        try:
            release = self._registry.active_release()
        except ActiveReleaseBoundError as exc:
            raise QueryPlanProposalError("semantic_release_not_bound") from exc
        if release is None:
            raise QueryPlanProposalError("no_active_semantic_release")
        if release.release_id != str(context.semantic_release_id):
            # The plan may only be parsed from the release the context was
            # compiled against; a rotated pointer fails closed.
            raise QueryPlanProposalError("context_release_mismatch")
        candidates = detect_metric_candidates(question, release, frozenset(context.asset_ids))
        match candidates:
            case ():
                raise QueryPlanProposalError("unknown_metric_selector")
            case (candidate,):
                pass
            case _:
                raise QueryPlanProposalError("ambiguous_metric_selector")
        proposal = parse_proposal(question, candidate)
        # Zero clock samples when no time clause is present (or when repeated
        # clauses already conflict); otherwise EXACTLY ONE sample for the whole
        # proposal, so every relative expression in it shares one now.
        if proposal.time_text is None or proposal.time_conflict:
            resolved: TimeRange | UnresolvedTime = UNRESOLVED_TIME
        else:
            resolved = resolve_time_expression(proposal.time_text, clock=self._clock)
        plan, _ = build_plan(proposal, resolved, context, candidate.contract)
        return plan


def parse_proposal(question: str, candidate: MetricCandidate) -> ProposalIntent:
    """Parse the closed zero-model grammar around one matched metric."""

    tokens = [token for token in _normalize(question).split() if token]
    if not tokens:
        raise QueryPlanProposalError("empty_question")
    claimed: set[str] = set()

    def claim(name: str) -> None:
        if name in claimed:
            raise QueryPlanProposalError(f"duplicate_clause:{name}")
        claimed.add(name)

    time_text: str | None = None
    time_conflict = False
    grain: Literal["day", "month"] = "day"
    dimension: str | None = None
    dimension_value: str | None = None
    intent: Literal["metric", "trend", "comparison", "ranking"] = "metric"
    result_limit: int | None = None
    filters: list[BoundFilter] = []
    scope_clauses: list[tuple[str, str | list[str]]] = []

    # The selector is always the leading token: either the explicit
    # metric=<asset_id|metric_key> form or the matched exact display-name/key.
    selector = tokens[0]
    selector_key, selector_separator, _ = selector.partition("=")
    if selector_separator and selector_key != "metric":
        raise QueryPlanProposalError(f"selector_required:{selector}")
    if not selector_separator and selector not in candidate.labels:
        raise QueryPlanProposalError(f"selector_required:{selector}")

    for token in tokens[1:]:
        scope_clause = _parse_scope_clause(token)
        if scope_clause is not None:
            name, raw_value = scope_clause
            _require_organization_scope_authorized(name)
            claim(f"scope.{name}")
            scope_clauses.append((name, _scope_values(raw_value)))
            continue
        key, separator, value = token.partition("=")
        if not separator:
            if token in _SCOPE_NAMES:
                # BARE form: routed through the same parser-side guard as the
                # <scope>=<value> and dim= forms, so the bare spelling cannot
                # bypass the organization-scope restriction.
                _require_organization_scope_authorized(token)
                claim("dim")
                dimension_value = token
                continue
            raise QueryPlanProposalError(f"unknown_clause:{token}")
        key = key.strip()
        value = value.strip()
        if key == "time":
            # Time handling splits exactly two ways:
            # * CLARIFICATION (unresolved TIME slot -> PlanValidator clarify ->
            #   no compile -> zero SQL -> zero model): a missing time clause
            #   (never reaching resolve_time_expression at all) and a repeated
            #   or conflicting clause (the conflict flag, clock sampled zero
            #   times), plus an unparseable / reversed / invalid-calendar
            #   expression returned by resolve_time_expression;
            # * PROPOSAL FAILURE (QueryPlanProposalError), NOT clarification: an
            #   EMPTY `time=` value raises empty_time_clause, exactly like an
            #   unknown clause, token or selector.
            if not value:
                raise QueryPlanProposalError("empty_time_clause")
            if "time" in claimed:
                time_conflict = True
            else:
                claimed.add("time")
            time_text = value
        elif key == "grain":
            claim("grain")
            if value not in _GRAINS:
                raise QueryPlanProposalError(f"unknown_grain:{value}")
            grain = cast(Literal["day", "month"], value)
        elif key == "dim":
            _require_organization_scope_authorized(value)
            claim("dim")
            dimension_value = value
        elif key == "intent":
            claim("intent")
            if value not in _INTENTS:
                raise QueryPlanProposalError(f"unknown_intent:{value}")
            intent = cast(Literal["metric", "trend", "comparison", "ranking"], value)
        elif key == "top":
            claim("top")
            result_limit = _top_value(value)
        elif key.startswith("filter."):
            field = key[len("filter.") :].strip()
            if not field:
                raise QueryPlanProposalError("unknown_filter_field")
            claim(f"filter.{field}")
            filters.append(_filter_clause(field, value, candidate.contract))
        else:
            raise QueryPlanProposalError(f"unknown_clause:{key}")

    grouped = intent in {"comparison", "ranking"}
    if dimension_value is not None:
        dimension = _dimension_value(dimension_value, grouped=grouped)
    for name, raw_value in scope_clauses:
        if name == "city_company":
            raise QueryPlanProposalError("city_company_dimension_rejected")
        dimension = dimension or name
        filters.append(_scope_filter(name, raw_value))

    if grouped and dimension is None:
        raise QueryPlanProposalError("grouped_intent_requires_dimension")
    if not grouped and dimension in set(_SCOPE_NAMES):
        raise QueryPlanProposalError("scope_requires_grouped_intent")
    if not grouped and result_limit is not None:
        raise QueryPlanProposalError("top_requires_ranking_intent")
    if intent == "ranking" and result_limit is None:
        result_limit = 10

    return ProposalIntent(
        candidate=candidate,
        time_text=time_text,
        time_conflict=time_conflict,
        grain=grain,
        dimension=dimension,
        intent=intent,
        result_limit=result_limit,
        filters=tuple(filters),
    )


def _parse_scope_clause(token: str) -> tuple[str, str] | None:
    if token == "city_company" or token.startswith("city_company="):
        raise QueryPlanProposalError("city_company_dimension_rejected")
    for name in (*_SCOPE_NAMES, "city_company"):
        if token.startswith(name + "="):
            return name, token[len(name) + 1 :].strip()
    match = _IN_CLAUSE.fullmatch(token)
    if match and match.group("name") in _SCOPE_NAMES:
        return match.group("name"), match.group("values").strip()
    return None


def _scope_values(raw: str) -> str | list[str]:
    if not raw:
        raise QueryPlanProposalError("empty_scope_value")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise QueryPlanProposalError("empty_scope_value")
    return values if len(values) > 1 else values[0]


def _require_no_organization_scope_in_intent(proposal: ProposalIntent) -> None:
    """Construction-boundary check for a forged ProposalIntent.

    Same single error code as the parser guard.  Both exist deliberately: the
    parser guard produces an early, well-labelled failure for text input, and
    this check closes the direct-construction route, so no caller - parser,
    test or future code - can reach an organization dimension or an
    ``entity_alias`` organization filter while the seam is absent.
    """

    if proposal.dimension in _SCOPE_NAMES:
        raise QueryPlanProposalError(_ORG_SCOPE_ERROR)
    for item in proposal.filters:
        if item.field_ref in _SCOPE_NAMES and item.source == "entity_alias":
            raise QueryPlanProposalError(_ORG_SCOPE_ERROR)


def _scope_filter(name: str, value: str | list[str]) -> BoundFilter:
    # Not reached from the parser while the organization-scope restriction
    # below stands, because the scope branch calls the guard first.  It is still
    # guarded so that this constructor builds an entity_alias predicate only for
    # a name the guard has already authorized; organization-scope names raise.
    _require_organization_scope_authorized(name)
    if isinstance(value, list):
        return BoundFilter(field_ref=name, operator="in", value=list(value), source="entity_alias")
    return BoundFilter(field_ref=name, operator="eq", value=value, source="entity_alias")


def _require_organization_scope_authorized(value: str) -> None:
    """Refuse an authorization-scoped organization clause, unconditionally.

    This is the single PARSER-SIDE enforcement helper and it has no Boolean
    gate.  Every parser route that could name an area/team/employee
    organization scope -- the ``<scope>=<value>`` filter form, the ``dim=<scope>``
    form and the bare ``<scope>`` form -- calls it, so no grammar input can
    emit an organization dimension or an ``entity_alias`` organization filter
    while the authorization seam is absent.

    It is not the only boundary: ``build_plan`` independently re-checks through
    ``_require_no_organization_scope_in_intent`` (and ``_scope_filter`` is
    guarded too), so a DIRECTLY constructed intent cannot bypass this helper.
    See the comment on ``_ORG_SCOPE_ERROR`` for why the restriction exists.
    """

    if value in _SCOPE_NAMES:
        raise QueryPlanProposalError(_ORG_SCOPE_ERROR)


def _dimension_value(value: str, *, grouped: bool) -> str:
    if value == "city_company":
        if grouped:
            raise QueryPlanProposalError("city_company_dimension_rejected")
        return value
    if value not in _SCOPE_NAMES:
        raise QueryPlanProposalError(f"unknown_dimension:{value}")
    return value


def _top_value(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise QueryPlanProposalError(f"malformed_top:{value}")
    number = int(value)
    if not 1 <= number <= 100:
        raise QueryPlanProposalError(f"top_out_of_range:{value}")
    return number


def _filter_clause(field: str, value: str, contract: MetricContract) -> BoundFilter:
    declared = {item.field: item.value_type for item in contract.filters}
    if field not in declared:
        raise QueryPlanProposalError(f"unknown_filter_field:{field}")
    if not value:
        raise QueryPlanProposalError(f"empty_filter_value:{field}")
    if declared[field] == "integer":
        if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
            raise QueryPlanProposalError(f"filter_type_mismatch:{field}")
        typed: Any = int(value)
    else:
        typed = value
    return BoundFilter(field_ref=field, operator="eq", value=typed, source="user")


def build_plan(
    proposal: ProposalIntent,
    resolved: TimeRange | UnresolvedTime,
    context: ContextBundle,
    contract: MetricContract,
) -> tuple[QueryPlan, tuple[str, ...]]:
    """Map one parsed proposal and its resolved time into a QueryPlan.

    Three-way behaviour, in evaluation order:

    1. ORGANIZATION SCOPE is rejected FIRST, before any slot evaluation, by the
       authorization-seam construction guard (``_require_no_organization_scope
       _in_intent``): an area/team/employee dimension or an ``entity_alias``
       organization filter raises ``org_scope_requires_authorization_seam``.
    2. An unsupported GRAIN becomes a CLARIFY slot.
    3. An unsupported NON-ORGANIZATION dimension becomes a CLARIFY slot.

    So clarify slots cover grain and non-organization dimensions; organization
    scope never reaches slot evaluation at all.

    SECOND ENFORCEMENT POINT (defense in depth).  The parser guard already
    refuses the recognized text routes to an organization scope, so this check
    is not reached from ``parse_proposal``; it exists so that a directly
    CONSTRUCTED ProposalIntent cannot bypass the parse boundary and emit an
    area/team/employee dimension or an ``entity_alias`` organization filter
    while the production authorization seam is absent.
    """

    del context
    _require_no_organization_scope_in_intent(proposal)
    unresolved: list[str] = []
    if proposal.time_conflict:
        # Two or more time clauses cannot be reconciled deterministically.
        # Conflicting time is clarification-possible, so it becomes an
        # unresolved slot rather than a proposal failure.
        unresolved.append("time")
    if isinstance(resolved, UnresolvedTime):
        if "time" not in unresolved:
            unresolved.append("time")
        # QueryPlan.time_range is a REQUIRED inclusive date pair under the frozen
        # contract, so an unresolved time slot still needs a well-formed carrier.
        # The carrier is a fixed, non-business 1970 placeholder and is inert ONLY
        # while this slot stays unresolved: the SLOT is the authority (PlanValidator
        # returns clarify, PlanCompiler refuses a non-allow record).  The carrier is
        # NOT intrinsically safe -- a caller that forged an allow record with the
        # slot cleared could compile it -- so nothing here may treat 1970 as a
        # business window, and no default/substituted period is ever inferred.
        carrier = _carrier_range()
    else:
        carrier = resolved

    if proposal.grain not in contract.supported_grains:
        unresolved.append("grain")
    # Currently not parser-reachable, for a reason that can change.
    # FACT (current grammar): with the organization-scope restriction in force,
    # the only dimension value the parser accepts is city_company (dim=city_company
    # for a non-grouped intent); area/team/employee are refused earlier.
    # FACT (current contract set): the two contracts in config/metrics/complaint.yaml
    # and the fixture contracts all declare city_company in supported_dimensions.
    # The parser cannot reach this branch WHILE those two facts both hold; a
    # future contract that omits city_company would make it parser-reachable.
    # It stays reachable regardless by a directly constructed ProposalIntent,
    # which is what test_build_plan_marks_an_unsupported_dimension_as_a_clarify_slot
    # covers.
    if proposal.dimension is not None and proposal.dimension not in contract.supported_dimensions:
        unresolved.append("dimension")

    grouped = proposal.intent in {"comparison", "ranking"}
    dimensions: tuple[str, ...] = (
        (proposal.dimension,) if grouped and proposal.dimension else ()
    )
    return (
        QueryPlan(
            intent=proposal.intent,
            domain=contract.domain,
            metric_keys=(proposal.candidate.asset_id,),
            dimensions=dimensions,
            filters=proposal.filters,
            time_range=carrier,
            grain=proposal.grain,
            source_strategy="aggregate_first",
            result_limit=proposal.result_limit,
            required_permissions=tuple(contract.required_permissions),
            unresolved_slots=tuple(dict.fromkeys(unresolved)),
        ),
        tuple(dict.fromkeys(unresolved)),
    )


def _carrier_range() -> TimeRange:
    # A deterministic, timezone-neutral carrier: the earliest legal ISO date.
    # It is inert only while the unresolved time slot is present, because the
    # slot is what stops compilation.  It is NOT intrinsically safe and carries
    # no business meaning: it is never an inferred or defaulted window.
    return TimeRange(start=date(1970, 1, 1), end=date(1970, 1, 1))


__all__ = [
    "DeterministicQueryPlanProvider",
    "ProposalIntent",
    "QueryPlanProposalError",
    "UNRESOLVED_TIME",
    "UnresolvedTime",
    "build_plan",
    "parse_proposal",
    "resolve_time_expression",
]
