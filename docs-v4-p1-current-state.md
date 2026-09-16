# V4 P1 metric inventory and current-state boundary

## Scope

P1 adds a read-only, deterministic inventory snapshot and a Planner-safe
projection.  It does not publish or activate metrics, execute SQL, join the
AuthoringIR/release checksum, call an online Planner/LLM, authorize access, or
write to PostgreSQL.

## Authoritative inputs

The files under `tests/fixtures/v4_p1/authoritative/` are byte-preserving
copies of the authoritative sibling sources.  The adapter reads their original
YAML/Markdown shapes; it does not create a flattened 294-row mirror.

Canonical Gold YAML: 10 files, 280 unique metrics, with raw=180,
derived=56, and external=44.  The per-file SHA-256 fingerprints are recorded
by the adapter in the immutable snapshot provenance.

The copied canonical file fingerprints are:

| File | SHA-256 |
| --- | --- |
| `complaint.yaml` | `15498384e2bd404a3d0dcb379c569182dba0040ec93134b14410298890e921a0` |
| `complaint_verification.yaml` | `a4f0e50f6b3ba37e7aca4a6fe59b205b8bd28f4237516ef312e827e28c2f3295` |
| `configured_external_metrics.yaml` | `f70f8cff43bbf0e746c88be6f2c13e64d9f0ce26d47d49bd93c215d001814d79` |
| `fault_delivery_external.yaml` | `416f7ea752085c74972f209bad0e29bae6228d533c715cfd6f01c2ae04ca4b49` |
| `inspection.yaml` | `7c95f2b4eb622a98857e67e188bab4b4beaf0b7308fa041d373f684308915e33` |
| `installation.yaml` | `3d228f19c6887ae44f0386680d295b4e7cc6368c90e1cb9e4cd59e7ff944d997` |
| `repair_service.yaml` | `f69b7479086a71e06d4f4b00823b67b64b082071994f3d8a3fa04fcad47226ad` |
| `satisfaction.yaml` | `452ccfd841ac5b510cf6afc4861b114fda2235bcdb7594b2c9e1de3de1b7685e` |
| `single_fault.yaml` | `5d4bb9d7e5b8d412eeab2711d180fddfdba61c2646994cd8539a42dba6cdb449` |
| `weak_light.yaml` | `7c81b4f8f69e0b60ed1d848fd7d355a4bdcefad5184b04d65358332c1871d867` |

Legacy `semantic.md`: 93 unique metric declarations.  The source contains 75
exact canonical overlaps and four explicit `*_day/month` compact declarations
that map to canonical day/month identities, giving 79 declared overlaps.  The
remaining 14 declarations are the only `legacy_semantic` records retained in
the inventory.  Compact alias handling is an explicit allowlist; an unknown
slash shape fails closed.

The legacy file SHA-256 is
`ba03630a273b1a907fde868b8ac55386bdd41a688aa79b6c4765338431d16f11`.

## Current state

The copied canonical YAML and legacy Markdown do not contain a current-state
contract.  Therefore inventory provenance alone produces `UNSPECIFIED` in the
Planner projection.  `CURRENT_STATE` is emitted only when a separate,
explicitly constructed `CurrentStateSnapshot` supplies a metric identity and a
state value.  This fixture does not claim production current-state evidence.

## Fingerprints

Inventory and projection fingerprints use independent, versioned canonical
JSON payloads with fixed schema/adapter versions, sorted identities,
provenance, and authoritative field fingerprints.  They are independent of
`AuthoringIR.checksum` and release materialization checksums.

The fixture snapshot fingerprint is
`6f8f9687eed492e126a30ac71f4936797a3ab93ef1b6c0d3d75c277ceeca31d0`; the
Planner projection fingerprint is
`eb082b113fca3bd1596a002dd7003291b65f6c3e8570a73dc084f45af6c77157`.
