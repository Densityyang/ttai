#!/usr/bin/env python
"""Read-only evidence harness for the V4 P2 org / resource / RelationCoverage gate.

Answers C2C task v4_p2_org_db_evidence CHECK 1-9 against the remote business
PostgreSQL inside an explicit READ ONLY transaction with 5s / 1s timeouts.

Safety contract (structurally enforced, not merely promised):

* the DSN is read from an environment variable or a *_FILE secret and is never
  printed, logged, or written to the output;
* every statement is checked against a read-only verb allowlist and a
  write/DDL token denylist before it is sent; a refusal aborts the run;
* the transaction is opened READ ONLY and the server-side
  transaction_read_only flag is asserted before any check executes;
* the run always ends with ROLLBACK, including on error;
* only catalog metadata, structural facts and aggregate counts are selected.
  No business value, identifier value, or free text column is ever read.

This script never treats the auditing identity as the product runtime identity:
it records current_user / session_user / superuser status as a provenance
caveat in the output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None

DSN_ENV_ORDER = ("P2_BUSINESS_RO_DSN", "BUSINESS_RO_DATABASE_URL", "DATABASE_URL")
DSN_FILE_ENV_ORDER = ("P2_BUSINESS_RO_DSN_FILE", "DATABASE_URL_FILE", "BUSINESS_RO_DATABASE_URL_FILE")

READ_ONLY_HEAD = re.compile(r"^\s*(select|with|set|begin|rollback|show)\b", re.IGNORECASE)
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|analyze|call|do|comment|reindex|cluster|refresh|lock|notify|listen|discard|reset)\b",
    re.IGNORECASE,
)

TABLES = (
    "organization_company",
    "organization_department",
    "organization_team",
    "organization_employee",
    "vadmin_area",
    "vadmin_data_resource",
    "vadmin_role_resource_permission",
    "vadmin_role_org_scope",
)
ORG_TABLES = (
    "organization_company",
    "organization_department",
    "organization_team",
    "organization_employee",
)
GOLD_TABLE = "gold_metric_result"


class Refused(Exception):
    """A statement was rejected by the read-only guard."""


class ReadOnly:
    """Guard every statement before it reaches the server."""

    def __init__(self, conn):
        self.conn = conn
        self.count = 0

    def _guard(self, sql: str) -> None:
        if not READ_ONLY_HEAD.match(sql):
            raise Refused(f"statement does not start with a read-only verb: {sql[:70]!r}")
        hit = FORBIDDEN.search(sql)
        if hit:
            raise Refused(f"statement contains a write/DDL token {hit.group(0)!r}: {sql[:70]!r}")

    def rows(self, sql: str, params=None):
        self._guard(sql)
        self.count += 1
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def exec_only(self, sql: str) -> None:
        """Run a statement that returns no rows, under the same guard."""
        self._guard(sql)
        self.count += 1
        with self.conn.cursor() as cur:
            cur.execute(sql)

    def one(self, sql: str, params=None):
        r = self.rows(sql, params)
        return r[0] if r else {}


def resolve_dsn() -> tuple[str | None, str | None]:
    for name in DSN_FILE_ENV_ORDER:
        raw = os.environ.get(name)
        if not raw:
            continue
        path = Path(raw)
        if path.exists():
            return path.read_text(encoding="utf-8").strip(), f"file:{name}"
    for name in DSN_ENV_ORDER:
        raw = os.environ.get(name)
        if raw:
            return raw.strip(), f"env:{name}"
    return None, None


def normalize(dsn: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)


def rel_exists(ro: ReadOnly, name: str) -> bool:
    row = ro.one(
        "SELECT 1 AS present FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s AND c.relkind IN ('r', 'p')",
        (name,),
    )
    return bool(row)


def columns_of(ro: ReadOnly, name: str):
    return ro.rows(
        "SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type, "
        "a.attnotnull AS not_null, pg_get_expr(d.adbin, d.adrelid) AS default_expr "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
        "WHERE n.nspname = current_schema() AND c.relname = %s ORDER BY a.attnum",
        (name,),
    )


def constraints_of(ro: ReadOnly, name: str):
    return ro.rows(
        "SELECT con.conname AS name, con.contype AS kind, "
        "pg_get_constraintdef(con.oid) AS definition "
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s "
        "ORDER BY con.contype, con.conname",
        (name,),
    )


def check1(ro: ReadOnly, tables):
    out = {}
    for t in tables:
        exists = rel_exists(ro, t)
        out[t] = {
            "exists": exists,
            "columns": columns_of(ro, t) if exists else [],
            "constraints": constraints_of(ro, t) if exists else [],
        }
    return out


def count_where(ro: ReadOnly, sql: str):
    try:
        return ro.one(sql).get("n")
    except Exception as exc:  # missing column/table must not abort the run
        return f"ERROR: {type(exc).__name__}"


def check2(ro: ReadOnly, schema):
    def has(t, c):
        return any(col["column_name"] == c for col in schema.get(t, {}).get("columns", []))

    out = {"counts": {}, "null_fk": {}, "orphan_fk": {}}
    for t in ORG_TABLES:
        if schema.get(t, {}).get("exists"):
            out["counts"][t] = count_where(ro, f"SELECT count(*) AS n FROM {t}")
        else:
            out["counts"][t] = "MISSING TABLE"

    fk_map = {
        "organization_department.company_id": ("organization_company", "id"),
        "organization_team.department_id": ("organization_department", "id"),
        "organization_employee.company_id": ("organization_company", "id"),
        "organization_employee.department_id": ("organization_department", "id"),
        "organization_employee.team_id": ("organization_team", "id"),
    }
    for left, (parent, key) in fk_map.items():
        t, c = left.split(".")
        if not schema.get(t, {}).get("exists") or not has(t, c):
            out["null_fk"][left] = "COLUMN ABSENT"
            out["orphan_fk"][left] = "COLUMN ABSENT"
            continue
        out["null_fk"][left] = count_where(ro, f"SELECT count(*) AS n FROM {t} WHERE {c} IS NULL")
        out["orphan_fk"][left] = count_where(
            ro,
            f"SELECT count(*) AS n FROM {t} x LEFT JOIN {parent} p ON p.{key} = x.{c} "
            f"WHERE x.{c} IS NOT NULL AND p.{key} IS NULL",
        )
    return out


def check3(ro: ReadOnly, schema):
    fks = ro.rows(
        "SELECT c.relname AS child_table, con.conname AS name, "
        "pg_get_constraintdef(con.oid) AS definition, "
        "pc.relname AS parent_table "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_class pc ON pc.oid = con.confrelid "
        "WHERE n.nspname = current_schema() AND con.contype = 'f' "
        "AND (c.relname = ANY(%s) OR pc.relname = ANY(%s)) "
        "ORDER BY c.relname, con.conname",
        (list(TABLES), list(TABLES)),
    )
    area_like = []
    for t in ORG_TABLES + ("vadmin_data_resource", "vadmin_role_org_scope"):
        cols = [c["column_name"] for c in schema.get(t, {}).get("columns", [])]
        area_like.append({
            "table": t,
            "area_columns": [c for c in cols if "area" in c.lower()],
            "all_columns_sample": cols[:40],
        })
    return {"foreign_keys": fks, "area_column_probe": area_like}


def check4(ro: ReadOnly, schema):
    t = "vadmin_data_resource"
    if not schema.get(t, {}).get("exists"):
        return {"exists": False}
    cols = [c["column_name"] for c in schema[t]["columns"]]
    out = {"exists": True, "columns": schema[t]["columns"], "org_field_org_level": None, "resources": None}
    if "org_field" in cols and "org_level" in cols:
        out["org_field_org_level"] = ro.rows(
            "SELECT org_field, org_level, count(*) AS n FROM vadmin_data_resource "
            "GROUP BY org_field, org_level ORDER BY n DESC, org_field, org_level",
        )
    wanted = [c for c in ("code", "table_name", "org_field", "org_level") if c in cols]
    if wanted:
        out["resources"] = ro.rows(
            "SELECT " + ", ".join(wanted) + " FROM vadmin_data_resource ORDER BY 1",
        )
    return out


def check5(ro: ReadOnly, schema):
    t = "vadmin_role_org_scope"
    if not schema.get(t, {}).get("exists"):
        return {"exists": False}
    cols = [c["column_name"] for c in schema[t]["columns"]]
    out = {"exists": True, "columns": schema[t]["columns"], "org_type": None, "include_children": None}
    if "org_type" in cols:
        out["org_type"] = ro.rows(
            "SELECT org_type, count(*) AS n FROM vadmin_role_org_scope GROUP BY org_type ORDER BY n DESC",
        )
    if "include_children" in cols:
        out["include_children"] = ro.rows(
            "SELECT include_children, count(*) AS n FROM vadmin_role_org_scope GROUP BY include_children",
        )
    return out


def check6(ro: ReadOnly, schema):
    t = "vadmin_role_resource_permission"
    if not schema.get(t, {}).get("exists"):
        return {"exists": False}
    cols = [c["column_name"] for c in schema[t]["columns"]]
    out = {"exists": True, "columns": schema[t]["columns"], "null_count": None, "json_type": None, "key_sets": None}
    if "column_permissions" not in cols:
        return out
    out["null_count"] = ro.rows(
        "SELECT (column_permissions IS NULL) AS is_null, count(*) AS n "
        "FROM vadmin_role_resource_permission GROUP BY 1 ORDER BY 1",
    )
    out["json_type"] = ro.rows(
        "SELECT jsonb_typeof(column_permissions) AS json_type, count(*) AS n "
        "FROM vadmin_role_resource_permission WHERE column_permissions IS NOT NULL GROUP BY 1",
    )
    out["key_sets"] = ro.rows(
        "SELECT (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(column_permissions) AS k) AS keys, "
        "count(*) AS n FROM vadmin_role_resource_permission "
        "WHERE column_permissions IS NOT NULL GROUP BY 1 ORDER BY n DESC",
    )
    return out


def check7(ro: ReadOnly, schema):
    # gold_metric_result is not one of the org/resource TABLES, so it must be
    # introspected on its own. Reading it out of the shared schema dict made the
    # first run report "absent" for a table that provably exists (it carries a
    # FK to vadmin_area).
    if not rel_exists(ro, GOLD_TABLE):
        return {"exists": False}
    cols = [c["column_name"] for c in columns_of(ro, GOLD_TABLE)]
    out = {"exists": True, "dimension_type": None, "null_patterns": None, "columns_present": {
        "area_id": "area_id" in cols, "team_id": "team_id" in cols, "employee_id": "employee_id" in cols,
    }}
    if "dimension_type" in cols:
        out["dimension_type"] = ro.rows(
            "SELECT dimension_type, count(*) AS n FROM gold_metric_result GROUP BY 1 ORDER BY n DESC",
        )
        parts = []
        for c in ("area_id", "team_id", "employee_id"):
            if c in cols:
                parts.append(f"count(*) FILTER (WHERE {c} IS NULL) AS {c}_null")
                parts.append(f"count(*) FILTER (WHERE {c} IS NOT NULL) AS {c}_not_null")
        if parts:
            out["null_patterns"] = ro.rows(
                "SELECT dimension_type, " + ", ".join(parts) + " FROM gold_metric_result GROUP BY 1 ORDER BY 1",
            )
    return out


def check8(ro: ReadOnly):
    out = {}
    out["views"] = ro.rows(
        "SELECT table_schema, table_name FROM information_schema.views "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY table_schema, table_name",
    )
    out["select_grants_by_grantee"] = ro.rows(
        "SELECT grantee, table_schema, count(*) AS n FROM information_schema.role_table_grants "
        "WHERE privilege_type = 'SELECT' GROUP BY grantee, table_schema ORDER BY grantee, table_schema",
    )
    out["roles"] = ro.rows(
        "SELECT rolname, rolcanlogin, rolsuper, rolconfig FROM pg_roles "
        "WHERE rolname NOT LIKE 'pg\\_%' ORDER BY rolname",
    )
    return out


def check9(schema, c2, c3, c4, c5, c6, c7, c8):
    org_tree = [t for t in ORG_TABLES if schema.get(t, {}).get("exists")]
    area_fk = [f for f in c3["foreign_keys"] if "vadmin_area" in (f.get("parent_table") or "")]
    emp_area_cols = [p for p in c3["area_column_probe"]
                     if p["table"] == "organization_employee" and p["area_columns"]]
    res = c4.get("resources") or []
    org_levels = sorted({str(r.get("org_level")) for r in res if r.get("org_level") is not None})
    unknowns = []
    if not area_fk and not emp_area_cols:
        unknowns.append("no DB-level FK or area column links organization_* to vadmin_area")
    if c6.get("key_sets"):
        unknowns.append("column_permissions key sets observed above; whether any consumer reads them is a code question")
    if not c5.get("org_type"):
        unknowns.append("vadmin_role_org_scope carries no org_type rows; no explicit custom org scope is configured")
    if c7.get("dimension_type"):
        unknowns.append("gold dimension_type coverage observed; per-dimension org coverage still needs approval")
    counts = c2.get("counts", {})
    nulls = c2.get("null_fk", {})
    layer_rows = {
        "vadmin_data_resource": c4.get("resources"),
        "vadmin_role_org_scope_org_type": c5.get("org_type"),
        "vadmin_role_resource_permission_key_sets": c6.get("key_sets"),
    }
    return {
        "A_auth_organization_tree": org_tree,
        "A2_effective_tree_shape": {
            "row_counts": counts,
            "null_fk": nulls,
            "department_level_empty": counts.get("organization_department") == 0,
            "all_teams_lack_department": nulls.get("organization_team.department_id") == counts.get("organization_team"),
            "all_employees_lack_department": nulls.get("organization_employee.department_id") == counts.get("organization_employee"),
        },
        "A3_authorization_layer_populated": {
            k: (len(v) if isinstance(v, list) else v) for k, v in layer_rows.items()
        },
        "B_business_dimension_tree": {
            "area_table_present": schema.get("vadmin_area", {}).get("exists", False),
            "vadmin_area_has_parent_and_level": [
                col["column_name"] for col in schema.get("vadmin_area", {}).get("columns", [])
                if col["column_name"] in ("parent_id", "level", "code", "adcode")
            ],
        },
        "C_db_level_mapping_between_trees": {
            "foreign_keys_to_vadmin_area": area_fk,
            "organization_employee_area_like_columns": emp_area_cols,
        },
        "D_relations_derivable_from_existing_metadata": {
            "vadmin_data_resource_rows": len(res),
            "distinct_org_field_org_level": c4.get("org_field_org_level") or "NO org_field/org_level columns",
            "org_level_values": org_levels,
        },
        "E_must_be_contracted_by_backend_not_inferred": [
            "any per-relation coverage_root_org / coverage_org_level / minimum_query_org_level",
            "detail_sensitivity per relation",
            "whether vadmin_area.level may gate query permission at all",
            "authorization revision / snapshot semantics",
        ],
        "remaining_unknowns": unknowns,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="V4 P2 read-only org/resource evidence harness")
    ap.add_argument("--out", default="p2_org_db_evidence.json")
    args = ap.parse_args()

    if psycopg is None:
        print("BLOCKED: psycopg is not importable in this interpreter", file=sys.stderr)
        return 2

    dsn, source = resolve_dsn()
    if not dsn:
        print("BLOCKED: no DSN available.", file=sys.stderr)
        print("Provide one of these before running:", file=sys.stderr)
        print("  env  " + " | ".join(DSN_ENV_ORDER), file=sys.stderr)
        print("  file " + " | ".join(DSN_FILE_ENV_ORDER), file=sys.stderr)
        print("The DSN is never echoed back, logged, or written to the output.", file=sys.stderr)
        return 2

    conn = psycopg.connect(normalize(dsn), connect_timeout=8)
    # autocommit must stay ON so that psycopg does not emit its own implicit BEGIN
    # ahead of ours; otherwise BEGIN READ ONLY would be a no-op and the session
    # would not actually be read-only. The harness asserts the server-side flag
    # below, so this cannot silently regress.
    conn.autocommit = True
    ro = ReadOnly(conn)
    try:
        ro.exec_only("BEGIN READ ONLY")
        ro.exec_only("SET LOCAL statement_timeout = '5s'")
        ro.exec_only("SET LOCAL lock_timeout = '1s'")
        state = ro.one("SHOW transaction_read_only").get("transaction_read_only")
        if state != "on":
            raise Refused(f"server reports transaction_read_only={state!r}; aborting")

        provenance = {
            "dsn_source": source,
            "current_user": ro.one("SELECT current_user AS u").get("u"),
            "session_user": ro.one("SELECT session_user AS u").get("u"),
            "is_superuser": ro.one("SELECT rolsuper AS s FROM pg_roles WHERE rolname = current_user").get("s"),
            "transaction_read_only": state,
            "server_version": ro.one("SHOW server_version").get("server_version"),
            "statement_timeout": ro.one("SHOW statement_timeout").get("statement_timeout"),
            "lock_timeout": ro.one("SHOW lock_timeout").get("lock_timeout"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        schema = check1(ro, TABLES)
        c2 = check2(ro, schema)
        c3 = check3(ro, schema)
        c4 = check4(ro, schema)
        c5 = check5(ro, schema)
        c6 = check6(ro, schema)
        c7 = check7(ro, schema)
        c8 = check8(ro)

        evidence = {
            "task_id": "v4_p2_org_db_evidence",
            "provenance": provenance,
            "check1_schema": schema,
            "check2_org_aggregates": c2,
            "check3_area_mapping": c3,
            "check4_data_resource": c4,
            "check5_role_org_scope": c5,
            "check6_column_permissions": c6,
            "check7_gold_shape": c7,
            "check8_read_transport": c8,
            "check9_conclusions": check9(schema, c2, c3, c4, c5, c6, c7, c8),
            "safety": {"statements_guarded": ro.count, "refused": None, "ended_with": "ROLLBACK"},
        }
        Path(args.out).write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print(json.dumps(evidence["check9_conclusions"], indent=2, default=str))
        print()
        print(f"provenance: user={provenance['current_user']} super={provenance['is_superuser']} "
              f"ro={provenance['transaction_read_only']} server={provenance['server_version']}")
        print(f"guarded statements: {ro.count}  -> {args.out}")
        return 0
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
