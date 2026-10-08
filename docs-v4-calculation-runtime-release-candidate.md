# V4 Calculation Runtime — Release Candidate

Status: **RELEASE CANDIDATE, production routing NOT_ENABLED.**
Scope: the shared typed calculation runtime and its migration from the legacy
Gold calculation subsystem.

Modules:

* `src/nl2sql/semantic/calculation_contract.py` — the bounded typed expression
  grammar and `CalculationSpec` / `CalculationExecutionBinding`.
* `src/nl2sql/semantic/calculation_runtime.py` — the pure deterministic
  evaluator (no I/O, no SQL, no database, no catalog, no authority, no routing).
* `src/nl2sql/orchestration/execution.py` — `RuntimeCalculationRunner`, the one
  reusable adapter from a validated step to the evaluator.

## 1. Migration classification

| Behaviour | Classification | Note |
|---|---|---|
| Typed deterministic expression evaluation | ADAPT_MIGRATE | the legacy `engine/expression.py` responsibility, now over the typed tree instead of generated SQL |
| In-process exact Decimal arithmetic | ADAPT_MIGRATE | legacy relied on PostgreSQL NUMERIC |
| Zero is a real value | DIRECT_MIGRATE | `0 + 0 = 0`, `0 * x = 0`, `0 / positive = 0` |
| Explicit COALESCE / CASE / NULL predicates | ADAPT_MIGRATE | the legacy `CASE … IS NULL … COALESCE` guards, now typed |
| Explicit ROUND(...) inside a formula | DIRECT_MIGRATE | part of the formula, never a global default |
| Divide-by-zero | **INTENTIONAL_REWRITE** | legacy `NULLIF(x,0) → NULL → NO_DATA`; V4 raises a typed calculation error |
| Legacy `_calculate_percentage` zero branch (0.00 / SUCCESS) | **RETIRE** | superseded by the frozen zero/undefined rules |
| Engine-injected NULLIF auto-protection | **NEW_BUILD** | never performed; `divide` semantics are authored, not injected |
| `AggregateOperand` runtime semantics | **NOT BUILT (deliberately)** | fails closed as unsupported |

## 2. Supported formula surface

Grammar (all nodes bounded, depth ≤ 16, expanded nodes ≤ 64, CASE/AND branches ≤ 8):

* `literal` (a finite numeric literal) and `null` (an explicitly **authored
  NULL result**, e.g. legacy `THEN NULL`) — an authored NULL is a value-state,
  never numeric zero, never NO_DATA, never false and never a failure
* `input` (role reference), `parameter` (per-run Parameter Contract)
* `add` / `subtract` / `multiply` / `divide`
* `round(expr, digits)` — explicit, authored, half-up, digits 0..12
* `coalesce(expr, literal)` — the substitute MUST be an author-stated literal
* `null_test` — `is_null` / `is_not_null`
* `compare` — `gt` / `le` only (the comparators the migrated formulas actually use)
* `all` — conjunction of comparisons
* `any` — disjunction of comparisons (no negation, no generic boolean language)
* `case(whens, otherwise)` — a legacy branch result `THEN NULL` and a legacy
  `ELSE NULL` are both represented by the explicit `null` node. An **absent**
  `otherwise` is also supported (it yields NULL) but is unexercised by the
  migrated corpus, which has zero absent-ELSE CASEs. Either way the result is a
  value-level NULL, which stays distinct from NO_DATA and from numeric zero.
  Note the authored-NULL *value-state* is preserved during evaluation (so
  `coalesce`/`null_test` can act on it), but it does **not** survive as a
  distinguishable *outcome*: `THEN NULL`, `ELSE NULL` and an unhandled input
  NULL all reach the boundary as `calculation_result_null_unsupported`.

* `CAST(x AS DECIMAL)` is NOT a node: every legacy expression contains it, and it
  is absorbed by the runtime's input/parameter coercion, which already yields an
  exact `Decimal`.

Condition semantics are **SQL-like three-valued**: a comparison with NULL is
UNKNOWN (not FALSE and not an error), `IS NULL`/`IS NOT NULL` are always TRUE
or FALSE, `all` is FALSE if any child is FALSE and UNKNOWN otherwise, `any` is
TRUE if any child is TRUE and UNKNOWN otherwise, and a CASE branch is selected
**only** by a TRUE condition. UNKNOWN is internal only: it is never no-data, a
calculation error, a numeric zero or a business false outside condition context.

Legacy coverage: **56 / 56 derived** expressions of the 280-metric canonical
Gold corpus are **grammar-representable** in the typed grammar **without any
semantic substitution** — an authored NULL (`THEN NULL` / `ELSE NULL`) is
never replaced by numeric zero. The corpus contains 48 CASE expressions, every
one closing with an explicit ELSE: 45 `ELSE NULL` plus 3 `THEN NULL`
(`repair_service.yaml`), each mapped to the `null` node. Source-level
`COALESCE(col, 0)` guards (38 sites, all inside THEN branches on a counter) are
reproduced as `coalesce` nodes, not as CASE defaults. This includes the three
`A IS NULL OR B IS NULL` guards in `repair_service.yaml` (which motivated
`any` and require an explicit `THEN NULL` result). The corpus split (raw = 180, derived = 56, external = 44) comes from
`docs-v4-p1-current-state.md`; the grammar claim is a **manual pass over that
inventory**, not an executed corpus-wide coverage test. Legacy formula coverage
is a *grammar* claim: it is NOT production activation, and it is not 56/280.

## 3. Result and error semantics

* Success → `EvaluationResult(value: Decimal, precision: int | None)`.
* `precision` is `None` when the spec declares no precision: **no implicit**
  `ROUND(...,2)` and no implicit scale is ever applied.
* Failure → `CalculationRuntimeError(code)`, secret-free and stable. The
  complete emitted set is exactly these 14 codes:
  `calculation_aggregate_unsupported`, `calculation_condition_unsupported`,
  `calculation_expression_too_deep`, `calculation_expression_too_large`,
  `calculation_expression_unsupported`, `calculation_input_missing`,
  `calculation_input_not_numeric`, `calculation_numeric_out_of_range`,
  `calculation_operator_unsupported`, `calculation_parameter_missing`,
  `calculation_parameter_not_numeric`, `calculation_result_null_unsupported`,
  `calculation_undefined`, `calculation_undefined_division_by_zero`.
* **VALUE and STATUS stay separate.** Undefined/errored calculations raise and
  never return a sentinel; no numeric value ever encodes status.
* Every `Decimal` trap (division by zero, invalid operation, overflow,
  underflow, subnormal, clamp) and the bounded-walk `ValueError` are converted
  into a typed `CalculationRuntimeError`: no raw decimal exception escapes.

## 4. Trust boundary

* The evaluator holds **no** authority, catalog, gateway, context or routing
  state, and never resolves a template or metric identity.
* Wrappers prove identity; the runtime proves arithmetic:
  `AdHocCalculationStep.validate_ad_hoc_semantics` proves binding↔spec identity
  and checksum agreement for AD_HOC, and `ApprovedCalculationCatalog` proves it
  for the canonical path.
* `CalculationSpec` structurally rejects authority and lifecycle fields, so the
  same spec can serve AD_HOC, Custom Definition and Published wrappers without
  carrying authority.
* Units are metadata only: the evaluator **never** scales arithmetic because of a
  unit (percent does not imply ×100). Scaling must be authored in the expression.

## 5. Known limitations

* `AggregateOperand` is declaration-only; it raises `calculation_aggregate_unsupported`.
* `null_policy` and `zero_policy` on `CalculationSpec` are **inert**: the runtime
  reads neither. This is deliberate (they conflict with the frozen zero/undefined
  rules) and their removal is a separate, versioned contract migration.
* A NULL operand reaching the boundary fails closed; explicit business NULL
  semantics belong in the expression (`coalesce` / `case` / `null_test`).
* Legacy unit identities (`分`, `积分`, `次/万户`, `次/万客户`) remain unmapped:
  no business identity was invented.
* No lock-free guarantee for Decimal spelling: identical numeric values written
  with different spellings have different spec checksums.

## 6. Validation evidence

Focused suites (runnable locally, exact commands):

    .venv\Scripts\python.exe -m pytest tests/unit/test_calculation_runtime.py       tests/unit/test_calculation_semantic_contract.py       tests/unit/test_ad_hoc_carrier.py tests/unit/test_ad_hoc_compiler.py       tests/unit/test_canonical_approved_compute.py       tests/unit/test_plan_pipeline.py -q

Full unit suite: `.venv\Scripts\python.exe -m pytest tests/unit -q`.

Static gates: `.venv\Scripts\python.exe -m ruff check src tests` and
`.venv\Scripts\python.exe -m pyright` (pyright covers `src` only).

Identity note: there is **no committed baseline** for the calculation contract in
this checkout (both modules are new, untracked files) and no previously published
spec/binding checksum exists, so "unchanged checksum" is not a claim about
history. To make identity stability *enforceable* rather than merely asserted,
`test_calculation_runtime.py` now pins the checksum of a fixed spec as an
explicit non-regression lock; every other checksum assertion in the suite is
reflexive.

Legacy test vectors are pinned against real historical definitions: the guarded
rate form, the plain ratio, the weighted blend (87.00 from 80/90), the
NULL-guarded mean, the nested-CASE composite, the `IS NULL OR IS NULL` repair
guard, and the intentional divide-by-zero divergence.

## 7. What is unsupported vs what is not enabled

* **Deliberate limitation:** `AggregateOperand` has no runtime semantics and
  fails closed as `calculation_aggregate_unsupported` (the seam supplies one
  already-aggregated scalar per role).
* **Pending cleanup (not a runtime gap):** `NullPolicy`/`ZeroPolicy` remain in
  the schema as inert fields; removing them is a separate versioned contract bump.
* **Blocked on business identity:** legacy units `分`, `积分`, `次/万户`,
  `次/万客户` have no agreed semantic identity and were not invented.
* **Grammar-complete but not executable:** the 180 legacy aggregation entries are
  declarations, not runtime aggregates — a direct consequence of the
  `AggregateOperand` limitation above, not an independent activation state.
* **NOT_ENABLED:** production routing, AD_HOC activation, catalog wiring and
  engine dispatch were never touched.

## 8. Production activation

**NOT_ENABLED.** No production routing, no AD_HOC activation, no catalog wiring
and no engine dispatch was added. The single wiring point for a future
activation is `metric_plan_executor` in `src/nl2sql/orchestration/metric_query.py`;
it deliberately passes no `ad_hoc_calculation_runner`, so an
`AdHocCalculationStep` fails closed with `ad_hoc_calculation_unavailable`.
