# PR07A Slice 3 — aggregate source selection and deterministic verification

## Delivery state

This coding gate extends the existing Slice 1/2 compiler and request-scoped
QueryGateway runner. Code and synthetic tests have been written and statically
reviewed. Ruff, Pyright, pytest, coverage, Docker integration, security checks,
Git and CI have **not** been run in this gate. No green gate or coverage level is
claimed. The PR remains subject to the user's separate validation and Git gates.

## Source authority and selection

Deployment-owned `RelationBinding` entries can share a semantic `source_ref`
while retaining unique source IDs. A compatible fresh aggregate is preferred;
otherwise an explicitly permitted stale/unknown fallback selects approved
detail. `detail_required` remains separately policy-controlled. No executable
source name, SQL expression or freshness declaration is accepted from QueryPlan.

An aggregate approval binds the complete metric definition and deployment
eligibility policy by SHA256, in addition to metric key, formula version and
operation. Changing additional eligibility predicates invalidates aggregate
compatibility even if a formula version label was reused. Selection still
requires permissions, active release relation membership, context approval,
validated snapshot coverage, allowed nonsensitive columns and expected types.
Physical mappings are typed identifiers; aggregate column roles must be distinct.
Source-specific checks now run before a source can win selection. An explicitly
allowed detail fallback also applies to a finite allowlist of permission,
relation, coverage, sensitivity, column/type, scan and freshness rejection codes;
the receipt records the exact safe code. A later fully valid fresh aggregate can
still win deterministically. Invalid global metric contracts/permissions never
fall back. If no source qualifies, an original structured rejection is returned.
Aggregate relations are restricted to `public` or `internal`; `restricted`,
`confidential`, mixed and unclassified sources are not ordinary aggregate inputs.

The generated aggregate query binds metric, formula, semantic release, snapshot,
source checkpoint, data watermark, daily grain, organization scope and the
half-open business period. Stable area/team IDs and filters retain Slice 2
validation. Count sums additive values; ratio sums numerator and denominator
before deriving an exact two-place 0–100 value. A zero denominator produces
NULL / `no_data`; stored display rates are never averaged. Trend, comparison
and ranking preserve the existing output shapes and ordering.

Detail scans require bounded business dates, a nonnegative snapshot row estimate
within the deployment cap, and a nonpartial index with business time as its
first column. An explicit bootstrap row cap is the only unindexed exception.
Arbitrary partition expressions, nonleading columns and partial indexes are
not accepted as evidence of bounded access. Runtime QueryGateway EXPLAIN,
read-only role, schema policy and execution limits remain mandatory.

Freshness comes from an injected trusted reader. Fresh evidence requires an
aware watermark and checkpoint tied to the current release/snapshot/checksum.
Fresh claims are reevaluated against the metric's positive freshness SLA using
an injected UTC clock, sampled once per compilation and again before execution.
Age equal to the SLA is accepted; older data is stale. Future watermarks or
observations are rejected. Optional `checked_at` must not precede the watermark
or follow evaluation time. Evaluation time never replaces `data_as_of` and is
not included in the source signature, so an unchanged fresh checkpoint remains
stable as the clock advances within its SLA.
Unknown detail freshness remains unknown. Immediately before execution the
runner rereads authority and recompiles; changes to SQL, parameters, freshness,
source, selection reason, degradation or semantic signature reject execution.
The semantic signature excludes physical source strategy and includes the data
checkpoint, so equal definitions on different checkpoints do not assert parity.

## Candidate evidence and decisions

`CandidateVerifier` consumes request-local evidence supplied by trusted compiler
and Gateway adapters. It is not a public model-output schema or an independent
authorization service. Ordered policy, semantic, SQL, EXPLAIN and execution
gates must all be present and pass. SQL policy/parameter checks are repeated;
fingerprint, semantic signature, source kind, estimated rows/cost and canonical
rowset evidence must match the execution receipt. A separately injected,
request-scoped expected-evidence map binds the full candidate metadata and exact
policy version, read-only role, source ID/checkpoint, selection reason and
degradation. The trusted compiler/Gateway adapter must register this expectation
before accepting candidate evidence; it must not construct authority from the
candidate's receipt. Missing expected authority fails closed. Typed boundary
validation converts malformed candidates, receipts or gates into safe failure
receipts without hiding internal policy/compiler exceptions. Invalid evidence produces a
safe failure receipt. Neither CandidateReceipt nor CandidateDecision carries SQL,
parameters or result values.

Fast and Standard accept at most one candidate. Deep accepts at most two only
within route execution/candidate budgets and explicit available capacity.
Capacity failure, zero candidates and duplicate IDs reject the decision; they do
not silently pick one result. After hard gates, different semantic signatures
or different typed rowset hashes require HITL. One surviving candidate can pass.
Equivalent passing candidates use lexicographic priority: aggregate, fewer
relations, fewer joins, fewer estimated rows, lower estimated cost, stable ID.
There are no confidence weights, score voting or agreement thresholds. Separate
routing risk/confidence signals are unchanged.

The capacity helper must be used before generation/execution by the future Deep
coordinator; checking it again at decision time cannot undo execution already
performed. This slice does not introduce parallel execution or expose arbitrary
candidate gate assertions through an application endpoint.

## Evidence authored for the validation gate

- Unit coverage for aggregate approval, permission/schema/type/coverage denial,
  freshness fallback, scan evidence, policy checksum changes and execution-time
  authority changes.
- Unit coverage for one selected source execution, safe source receipts,
  malformed candidate evidence, deterministic priorities, route/capacity limits,
  semantic/rowset divergence and absence of weighted scoring in online candidate
  code. The architecture assertion deliberately excludes routing risk logic.
- PostgreSQL synthetic daily facts generated from the fixture's detail rows,
  under fixed formula/release/snapshot/checkpoint IDs. Count/rate, daily/monthly
  trends, comparison and ranking compare outputs and canonical rowset hashes.
  Fresh/stale/unknown cases exercise selection, and rows from another checkpoint
  must not contribute to aggregate results. Each request accounts one SQL
  execution through QueryGateway.
- ExecutionReceipt source fields are optional for existing consumers; the typed
  executor propagates safe fields into PlanStepReceipt without SQL or row data.

These fixtures are **not enterprise Gold/Silver parity proof**. Their approval,
snapshot and watermark readers are synthetic test authorities; they do not
demonstrate a production publication, ingestion checkpoint or freshness SLA.

## Deferred scope and handoff

The next slice owns actual Deep capacity coordination, same-checkpoint
Gold/Silver parity runner and enterprise readiness/DoD. Production aggregate
materialization must establish the approved daily facts' uniqueness, correctness,
eligibility and checkpoint immutability; a runtime checksum cannot prove the
underlying data was built correctly. Enterprise DB access, real Gold migrations,
AppContainer wiring, natural-language routing, PR07B and legacy paths are outside
this coding gate.

Luna validation should run the affected unit suites and existing PostgreSQL
gateway integration matrix using the project-locked uv environment, then the
required lint/type/coverage/security gates. Record actual new-core coverage
against the 90% target and investigate uncovered behavior before declaring the
gate green. Keep validation, commit, push, PR/CI inspection and merge separate;
do not replace `.venv` or introduce enterprise data for these tests.
