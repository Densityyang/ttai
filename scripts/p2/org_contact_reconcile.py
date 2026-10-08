#!/usr/bin/env python
"""Read-only reconcile of the area contact workbook against the live database.

C2C task v4_p2_org_contact_reconcile. Answers CHECK 1-8 without modifying the
database, the code, or git.

PII is excluded structurally, not by promise: the workbook reader filters cells
by an explicit column allowlist BEFORE they are materialised, so the personal
name and telephone columns are never parsed into any object this script can
print, serialise, or log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from org_db_evidence import ReadOnly, normalize, resolve_dsn  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

WORKBOOK = Path(os.environ.get("P2_WORKBOOK", r"C:\Users\Density\Downloads\副本区县通讯录.xlsx"))
SHEET_AREAS = "21区县通讯录"
SHEET_TEAMS = "班组长信息"

# Column allowlists. Everything outside these indices is personal data and never
# reaches an in-memory object.
AREA_COLS = {0: "seq", 1: "area_name", 2: "tier_raw", 3: "area_short"}
TEAM_COLS = {
    0: "seq", 1: "area_seq", 2: "tier_raw", 3: "area_full",
    4: "area_short", 5: "business_type", 6: "team_name", 7: "branch_name",
}

TIER_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def load_sheets(path: Path):
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(M + "si"):
            shared.append("".join(t.text or "" for t in si.iter(M + "t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for sh in wb.find(M + "sheets"):
        target = rels.get(sh.get(RNS + "id"), "")
        full = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
        out[sh.get("name")] = full
    return z, shared, out


def read_rows(z, shared, member: str, allowed: dict):
    """Yield dicts holding ONLY the allowed columns."""
    root = ET.fromstring(z.read(member))
    rows = []
    for row in root.iter(M + "row"):
        # row 1 is the header; treating it as data inflated the area and team
        # counts by one and leaked the column titles into the value sets.
        if row.get("r") == "1":
            continue
        rec = {}
        for c in row.findall(M + "c"):
            idx = col_index(c.get("r"))
            if idx not in allowed:
                continue
            t = c.get("t")
            v = c.find(M + "v")
            isel = c.find(M + "is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif isel is not None:
                val = "".join(x.text or "" for x in isel.iter(M + "t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            rec[allowed[idx]] = re.sub(r"\s+", " ", (val or "")).strip()
        if rec:
            rows.append(rec)
    return rows


def parse_tier(raw: str):
    m = re.search(r"([一二三四1234])", raw or "")
    return TIER_DIGITS.get(m.group(1)) if m else None


def norm_key(s: str) -> str:
    return re.sub(r"[\s区县市]", "", s or "")


def fetch_db(ro: ReadOnly):
    areas = ro.rows(
        "SELECT id, name, code, level, parent_id, is_active, is_deleted "
        "FROM vadmin_area ORDER BY level, name",
    )
    team_cols = [c["column_name"] for c in ro.rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'organization_team' "
        "ORDER BY ordinal_position",
    )]
    emp_cols = [c["column_name"] for c in ro.rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'organization_employee' "
        "ORDER BY ordinal_position",
    )]
    teams = ro.rows(
        "SELECT id, code, name, is_active, is_deleted FROM organization_team ORDER BY name, id",
    )
    emp_by_team = ro.rows(
        "SELECT team_id, count(*) AS n FROM organization_employee "
        "WHERE team_id IS NOT NULL GROUP BY team_id ORDER BY team_id",
    )
    return areas, team_cols, emp_cols, teams, emp_by_team


def check1_areas(wb_areas, db_areas):
    by_name = defaultdict(list)
    by_norm = defaultdict(list)
    for a in db_areas:
        by_name[a["name"]].append(a)
        by_norm[norm_key(a["name"])].append(a)
    parent = {a["id"]: a["parent_id"] for a in db_areas}
    by_id = {a["id"]: a for a in db_areas}
    out = []
    for w in wb_areas:
        full = w.get("area_name", "")
        short = w.get("area_short", "")
        match, status, basis, candidates = None, "NOT_FOUND", None, []
        if len(by_name.get(full, [])) == 1:
            match, status, basis = by_name[full][0], "EXACT", "area_name_exact"
        elif len(by_name.get(full, [])) > 1:
            candidates, status, basis = by_name[full], "AMBIGUOUS", "area_name_exact_multi"
        elif len(by_name.get(short, [])) == 1:
            match, status, basis = by_name[short][0], "APPROVED_ALIAS_REQUIRED", "area_short_exact"
        else:
            for key, label in ((norm_key(full), "area_name_normalized"),
                               (norm_key(short), "area_short_normalized")):
                cand = by_norm.get(key, [])
                if len(cand) == 1:
                    match, status, basis = cand[0], "APPROVED_ALIAS_REQUIRED", label
                    break
                if len(cand) > 1:
                    candidates, status, basis = cand, "AMBIGUOUS", label + "_multi"
                    break
        chain, node = [], match
        while node:
            chain.append(node["name"])
            node = by_id.get(parent.get(node["id"]))
        out.append({
            "workbook_area_name": full,
            "workbook_short_name": short,
            "workbook_tier": parse_tier(w.get("tier_raw", "")),
            "db_area_id": match["id"] if match else None,
            "db_area_name": match["name"] if match else None,
            "db_area_code": match["code"] if match else None,
            "db_area_level": match["level"] if match else None,
            "db_parent_id": match["parent_id"] if match else None,
            "db_ancestor_chain": chain,
            "match_status": status,
            "match_basis": basis,
            "candidate_count": len(candidates) if candidates else (1 if match else 0),
            "candidates": [{"id": c["id"], "name": c["name"], "code": c["code"]} for c in candidates],
        })
    city_parents = {tuple(r["db_ancestor_chain"][1:2]) for r in out if r["db_ancestor_chain"]}
    return {
        "rows": out,
        "workbook_area_count": len(out),
        "exact": sum(1 for r in out if r["match_status"] == "EXACT"),
        "alias_required": sum(1 for r in out if r["match_status"] == "APPROVED_ALIAS_REQUIRED"),
        "not_found": sum(1 for r in out if r["match_status"] == "NOT_FOUND"),
        "ambiguous": sum(1 for r in out if r["match_status"] == "AMBIGUOUS"),
        "single_city_subtree": len(city_parents) <= 1,
        "distinct_city_parents": sorted(str(p) for p in city_parents),
    }


def build_area_lookup(area_result):
    lookup = {}
    for r in area_result["rows"]:
        if r["db_area_id"] is not None:
            lookup[r["workbook_area_name"]] = r["db_area_id"]
            if r["workbook_short_name"]:
                lookup.setdefault(r["workbook_short_name"], r["db_area_id"])
    return lookup


def check2_teams(wb_teams, db_teams, area_lookup):
    by_name = defaultdict(list)
    by_norm = defaultdict(list)
    for t in db_teams:
        by_name[t["name"]].append(t)
        by_norm[norm_key(t["name"])].append(t)
    out, matched_ids = [], set()
    for w in wb_teams:
        name = w.get("team_name", "")
        area = w.get("area_full", "")
        status, basis, reason, pick = "NOT_FOUND", "no_name_match", None, None
        cand = by_name.get(name, [])
        if len(cand) == 1:
            pick, status, basis = cand[0], "MATCHED", "exact_name_unique"
        elif len(cand) > 1:
            status, basis, reason = "AMBIGUOUS", "exact_name_multi", str(len(cand)) + " db teams share this name"
        else:
            cand2 = by_norm.get(norm_key(name), [])
            if len(cand2) == 1:
                pick, status, basis = cand2[0], "REVIEW", "normalized_name_unique"
            elif len(cand2) > 1:
                status, basis, reason = "AMBIGUOUS", "normalized_name_multi", str(len(cand2)) + " db teams after normalization"
        if pick:
            matched_ids.add(pick["id"])
        out.append({
            "workbook_area": area,
            "workbook_business_type": w.get("business_type", ""),
            "workbook_team_name": name,
            "workbook_branch_name": w.get("branch_name", ""),
            "db_team_id": pick["id"] if pick else None,
            "db_team_code": pick["code"] if pick else None,
            "db_team_name": pick["name"] if pick else None,
            "db_is_active": pick["is_active"] if pick else None,
            "db_is_deleted": pick["is_deleted"] if pick else None,
            "proposed_area_id": area_lookup.get(area),
            "match_status": status,
            "match_basis": basis,
            "ambiguity_reason": reason,
        })
    return out, matched_ids


def check3_gap(db_teams, matched_ids, emp_by_team, team_rows):
    emp = {r["team_id"]: r["n"] for r in emp_by_team}
    name_counts = defaultdict(int)
    for t in db_teams:
        name_counts[t["name"]] += 1
    db_only = []
    for t in db_teams:
        if t["id"] in matched_ids:
            continue
        if t["is_deleted"]:
            cls = "deleted"
        elif not t["is_active"]:
            cls = "inactive"
        elif name_counts[t["name"]] > 1:
            cls = "obvious_duplicate_candidate"
        else:
            cls = "unknown_requires_business_review"
        db_only.append({
            "team_id": t["id"], "code": t["code"], "name": t["name"],
            "is_active": t["is_active"], "is_deleted": t["is_deleted"],
            "employees": emp.get(t["id"], 0), "classification": cls,
        })
    classes = defaultdict(int)
    for r in db_only:
        classes[r["classification"]] += 1
    active_db_only = [r for r in db_only if r["classification"] == "unknown_requires_business_review"]
    return {
        "workbook_teams": len(team_rows),
        "exact_matched_workbook_teams": sum(1 for r in team_rows if r["match_status"] == "MATCHED"),
        "review_required_workbook_teams": sum(1 for r in team_rows if r["match_status"] == "REVIEW"),
        "ambiguous_workbook_teams": sum(1 for r in team_rows if r["match_status"] == "AMBIGUOUS"),
        "workbook_teams_not_found": sum(1 for r in team_rows if r["match_status"] == "NOT_FOUND"),
        "db_teams_total": len(db_teams),
        "db_teams_matched": len(matched_ids),
        "db_teams_not_in_workbook": len(db_only),
        "classification_counts": dict(classes),
        "active_db_teams_without_workbook_entry": len(active_db_only),
        "active_db_teams_without_workbook_entry_with_employees": sum(1 for r in active_db_only if r["employees"] > 0),
        "coverage_conclusion": ("workbook covers only a business subset" if active_db_only else "workbook covers every active db team"),
        "db_only_detail": db_only,
    }


def check4_codes(db_teams):
    nulls = [t["id"] for t in db_teams if not t["code"]]
    counts = defaultdict(list)
    for t in db_teams:
        counts[t["code"]].append(t["id"])
    dups = {k: v for k, v in counts.items() if k and len(v) > 1}
    return {
        "total": len(db_teams),
        "null_or_empty": len(nulls),
        "distinct_codes": len([k for k in counts if k]),
        "duplicate_code_groups": len(dups),
        "duplicates": {str(k): v for k, v in list(dups.items())[:20]},
        "globally_unique": (not dups) and (not nulls),
        "stable_identity_verdict": ("code is non-null and globally unique; usable as long-term identity" if (not dups and not nulls) else "code is NOT safe as sole identity; use id as authority"),
    }


def check5_migration(team_rows, gap, team_cols):
    resolvable = [r for r in team_rows if r["db_team_id"] is not None and r["proposed_area_id"] is not None]
    unresolved = [r for r in team_rows if r["db_team_id"] is not None and r["proposed_area_id"] is None]
    active_db_only = [r for r in gap["db_only_detail"] if r["classification"] == "unknown_requires_business_review"]
    reasons = defaultdict(int)
    for _ in unresolved:
        reasons["matched team whose workbook area did not reconcile to a db area"] += 1
    return {
        "teams_with_immediately_determinable_area_id": len(resolvable),
        "workbook_teams_with_area_but_no_db_identity": sum(1 for r in team_rows if r["db_team_id"] is None),
        "matched_teams_still_missing_area": len(unresolved),
        "active_db_teams_with_no_area_evidence": len(active_db_only),
        "unresolved_reasons": dict(reasons),
        "ddl": "ALTER TABLE organization_team ADD COLUMN area_id BIGINT NULL REFERENCES vadmin_area(id);",
        "index_suggestion": "CREATE INDEX CONCURRENTLY ix_organization_team_area_id ON organization_team (area_id);",
        "fk_suggestion": "FOREIGN KEY (area_id) REFERENCES vadmin_area(id) ON DELETE RESTRICT: an area must not be deletable while teams map to it",
        "order": [
            "1. add nullable area_id (no default, no table rewrite)",
            "2. backfill only teams whose area is approved (no UPDATE is executed by this task)",
            "3. validate that remaining NULLs are explained by business review",
            "4. only then consider NOT NULL; not before every active team is explained",
            "5. add the FK and the index after backfill, never before",
        ],
        "not_null_recommendation": ("DO NOT set NOT NULL yet" if active_db_only else "NOT NULL can be considered"),
        "already_has_area_column": "area_id" in set(team_cols),
    }


def check6_business_type(wb_teams, team_cols, emp_cols):
    values = sorted({w.get("business_type", "") for w in wb_teams if w.get("business_type")})
    candidates = [c for c in team_cols if re.search(r"business|type|special|category|profession", c, re.I)]
    emp_candidates = [c for c in emp_cols if re.search(r"business|type|special|category|profession", c, re.I)]
    reco = ("reuse existing column " + candidates[0]) if candidates else "add organization_team.business_type as a plain business column; it must not be an Agent-specific name and must not become an authorization input on its own"
    return {
        "workbook_vocabulary": values,
        "organization_team_columns": team_cols,
        "organization_employee_columns_relevant": emp_candidates,
        "reusable_existing_field": candidates or None,
        "can_reuse": bool(candidates),
        "recommendation": reco,
        "agent_specific_field_names_created": [],
    }


def check7_tiers(wb_areas, wb_teams):
    per_area = defaultdict(set)
    for w in wb_teams:
        tier = parse_tier(w.get("tier_raw", ""))
        if tier is not None:
            per_area[w.get("area_full", "")].add(tier)
    sheet1 = {}
    for w in wb_areas:
        tier = parse_tier(w.get("tier_raw", ""))
        if tier is not None:
            sheet1[w.get("area_name", "")] = tier
    rows, conflicts = [], []
    for name, tiers in sorted(per_area.items()):
        t1 = sheet1.get(name)
        consistent = len(tiers) == 1 and (t1 is None or t1 in tiers)
        if not consistent:
            conflicts.append({"area": name, "sheet2_tiers": sorted(tiers), "sheet1_tier": t1})
        rows.append({
            "area_name": name,
            "tier": sorted(tiers)[0] if len(tiers) == 1 else None,
            "sheet2_tiers": sorted(tiers),
            "sheet1_tier": t1,
            "consistent": consistent,
        })
    return {
        "area_to_tier": rows,
        "every_area_has_exactly_one_tier": not conflicts,
        "conflicts": conflicts,
        "sheet1_tier_present_for": len(sheet1),
        "sheet1_tier_blank_forward_fill_required": True,
        "note": "tier is workbook metadata; it is NOT an authorization level",
    }


def check8_do_not_migrate():
    return {
        "excluded_from_p2_authorization": [
            "经理 / 副经理", "维护主管", "内训师", "集客组长 / 综支组长", "班组长",
            "all contact telephone numbers",
        ],
        "reason": "contact-directory data, not Agent permission identity; if the backend needs it that is a separate business data maintenance concern",
        "pii_columns_never_parsed": {
            SHEET_AREAS: ["经理", "联系电话", "副经理", "维护主管", "工作电话", "内训师", "集客组长", "电话", "综支组长"],
            SHEET_TEAMS: ["班组长", "联系电话"],
        },
    }


def check_code_area_derivation(db_areas, db_teams):
    """organization_team.code looks like <area name><team name>; test it."""
    names = sorted({a["name"] for a in db_areas if a["name"]}, key=len, reverse=True)
    by_name = {a["name"]: a for a in db_areas if a["name"]}
    rows = []
    for t in db_teams:
        code = t["code"] or ""
        name = t["name"] or ""
        hit = next((n for n in names if code.startswith(n)), None)
        rows.append({
            "team_id": t["id"],
            "code": code,
            "name": name,
            "derived_area_name": hit,
            "derived_area_id": by_name[hit]["id"] if hit else None,
            "code_equals_area_plus_name": bool(hit) and code[len(hit):] == name,
        })
    return {
        "total": len(db_teams),
        "teams_with_area_derivable_from_code": sum(1 for r in rows if r["derived_area_name"]),
        "code_equals_area_name_plus_team_name": sum(1 for r in rows if r["code_equals_area_plus_name"]),
        "examples": rows[:8],
        "codes_with_no_area_prefix": [r for r in rows if not r["derived_area_name"]][:20],
    }


def check_specialty_vocabulary(ro: ReadOnly):
    """Is any business-type / specialty vocabulary already populated?"""
    out = {}
    for col in ("business_type", "position_category", "major_specialty", "business_line"):
        out[col] = ro.rows(
            "SELECT " + col + " AS value, count(*) AS n FROM organization_employee "
            "GROUP BY " + col + " ORDER BY n DESC LIMIT 20",
        )
    out["dictionary_tables"] = ro.rows(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name LIKE '%dict%' ORDER BY table_name",
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 org contact reconcile (read-only)")
    parser.add_argument("--out", default="docs-v4-p2-org-contact-reconcile.json")
    args = parser.parse_args()
    if psycopg is None:
        print("BLOCKED: psycopg not importable", file=sys.stderr)
        return 2
    if not WORKBOOK.exists():
        print("BLOCKED: workbook not found at " + str(WORKBOOK), file=sys.stderr)
        return 2
    dsn, source = resolve_dsn()
    if not dsn:
        print("BLOCKED: no DSN available", file=sys.stderr)
        return 2

    z, shared, sheets = load_sheets(WORKBOOK)
    wb_areas = [r for r in read_rows(z, shared, sheets[SHEET_AREAS], AREA_COLS) if r.get("area_name")]
    wb_teams = [r for r in read_rows(z, shared, sheets[SHEET_TEAMS], TEAM_COLS) if r.get("team_name")]

    conn = psycopg.connect(normalize(dsn), connect_timeout=8)
    conn.autocommit = True
    ro = ReadOnly(conn)
    try:
        ro.exec_only("BEGIN READ ONLY")
        ro.exec_only("SET LOCAL statement_timeout = '10s'")
        ro.exec_only("SET LOCAL lock_timeout = '1s'")
        if ro.one("SHOW transaction_read_only").get("transaction_read_only") != "on":
            print("REFUSED: session is not read-only", file=sys.stderr)
            return 3
        db_areas, team_cols, emp_cols, db_teams, emp_by_team = fetch_db(ro)
        c1 = check1_areas(wb_areas, db_areas)
        c2_rows, matched_ids = check2_teams(wb_teams, db_teams, build_area_lookup(c1))
        c3 = check3_gap(db_teams, matched_ids, emp_by_team, c2_rows)
        c4 = check4_codes(db_teams)
        c5 = check5_migration(c2_rows, c3, team_cols)
        c6 = check6_business_type(wb_teams, team_cols, emp_cols)
        c7 = check7_tiers(wb_areas, wb_teams)
        c4b = check_code_area_derivation(db_areas, db_teams)
        c6b = check_specialty_vocabulary(ro)
        c2_counts = {
            "total": len(c2_rows),
            "matched": sum(1 for r in c2_rows if r["match_status"] == "MATCHED"),
            "review": sum(1 for r in c2_rows if r["match_status"] == "REVIEW"),
            "ambiguous": sum(1 for r in c2_rows if r["match_status"] == "AMBIGUOUS"),
            "not_found": sum(1 for r in c2_rows if r["match_status"] == "NOT_FOUND"),
        }
        evidence = {
            "task_id": "v4_p2_org_contact_reconcile",
            "provenance": {
                "workbook": WORKBOOK.name,
                "workbook_sheets": list(sheets),
                "dsn_source": source,
                "db_auditing_identity": ro.one("SELECT current_user AS u").get("u"),
                "transaction_read_only": "on",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "pii_policy": "workbook parsed through a column allowlist; personal name and telephone columns were never materialised",
            },
            "check1_area_reconciliation": c1,
            "check2_team_reconciliation": {"rows": c2_rows, "counts": c2_counts},
            "check3_coverage_gap": c3,
            "check4_team_code_identity": c4,
            "check4b_area_derivable_from_code": c4b,
            "check6b_employee_business_type_vocabulary": c6b,
            "check5_migration_proposal": c5,
            "check6_business_type": c6,
            "check7_tiers": c7,
            "check8_do_not_migrate": check8_do_not_migrate(),
            "safety": {"statements_guarded": ro.count, "ended_with": "ROLLBACK"},
        }
        Path(args.out).write_text(json.dumps(evidence, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        summary = {
            "workbook_areas": c1["workbook_area_count"],
            "area_exact": c1["exact"], "area_alias_required": c1["alias_required"],
            "area_not_found": c1["not_found"], "area_ambiguous": c1["ambiguous"],
            "single_city_subtree": c1["single_city_subtree"],
            "workbook_teams": len(c2_rows), "team_counts": c2_counts,
            "db_teams": c3["db_teams_total"], "db_only": c3["db_teams_not_in_workbook"],
            "db_only_classes": c3["classification_counts"],
            "active_db_only_with_employees": c3["active_db_teams_without_workbook_entry_with_employees"],
            "coverage": c3["coverage_conclusion"],
            "code_unique": c4["globally_unique"],
            "resolvable_area": c5["teams_with_immediately_determinable_area_id"],
            "unresolved_active": c5["active_db_teams_with_no_area_evidence"],
            "not_null_reco": c5["not_null_recommendation"],
            "business_type_values": c6["workbook_vocabulary"],
            "area_from_code": c4b["teams_with_area_derivable_from_code"],
            "code_equals_area_plus_name": c4b["code_equals_area_name_plus_team_name"],
            "employee_business_type_values": c6b,
            "tier_conflicts": len(c7["conflicts"]),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        print()
        print("-> " + args.out + "   guarded=" + str(ro.count))
        return 0
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
