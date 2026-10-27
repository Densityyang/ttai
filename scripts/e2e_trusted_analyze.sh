#!/usr/bin/env bash
# End-to-end verification of the trusted typed ANALYZE path.
# Usage: bash scripts/e2e_trusted_analyze.sh <telephone> <password>
set -euo pipefail

TELEPHONE="${1:?telephone required}"
PASSWORD="${2:?password required}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

step() { printf '\n==> %s\n' "$*"; }

step "1. login via tt-api (backend nginx on 127.0.0.1:12080 routes /vadmin)"
LOGIN_BODY=$(curl -s -X POST "http://177.8.0.2:9000/auth/api/login" \
  -H 'accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "grant_type=password&username=${TELEPHONE}&password=${PASSWORD}&scope=&client_id=string&client_secret=********")
TOKEN=$(printf '%s' "$LOGIN_BODY" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("access_token") or d.get("data",{}).get("access_token") or "")')
[ -n "$TOKEN" ] || { echo "login failed: $LOGIN_BODY" >&2; exit 1; }
echo "login ok"

step "2. tt-api agent authorization snapshot"
curl -s "http://177.8.0.2:9000/vadmin/auth/agent/authorization" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

step "3. POST /api/v2/nl2sql/queries/stream (ANALYZE)"
curl -sN -X POST "$BASE_URL/api/v2/nl2sql/queries/stream" \
  -H 'accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "messages": [{"role": "user", "content": "查询最新可用日期的报修服务归档及时率"}],
    "thread_id": "3f2a9c1e-7b4d-4e8a-9c6f-1a2b3c4d5e6f",
    "requested_mode": "ANALYZE"
  }' | head -c 4000
printf '\n'
