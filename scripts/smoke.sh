#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${SMOKE_BASE_URL:-http://127.0.0.1:${TTAI_PORT:-8000}}"
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-10}"

check_endpoint() {
  local path="$1"
  local expected_status="$2"
  local body
  body="$(curl --fail --silent --show-error --max-time "$TIMEOUT" --retry 3 --retry-connrefused \
    --retry-delay 1 "$BASE_URL$path")" || {
    printf 'smoke failed: %s\n' "$path" >&2
    return 1
  }
  [[ "$body" == *"\"status\":\"$expected_status\""* ]] || {
    printf 'smoke failed: %s returned an unexpected status\n' "$path" >&2
    return 1
  }
}

check_endpoint /healthz ok
check_endpoint /readyz ready
printf 'smoke passed via %s\n' "$BASE_URL"
