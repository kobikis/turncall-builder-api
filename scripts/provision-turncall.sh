#!/usr/bin/env bash
# Provision a TurnCall project + admin API key and write it into .env.
#
# TurnCall's local DB is wiped on `make docker-down` (--volumes), which
# invalidates the builder's key (401 on create). Run this after (re)starting
# TurnCall to mint a fresh key. Idempotent enough — it just creates a new
# project+key each run and rewrites the TURNCALL_API_KEY line.
set -euo pipefail

BASE="${TURNCALL_HOST_URL:-http://localhost:8090}"   # TurnCall API as seen from the host

# Project/key bootstrap is platform-gated (turncall#102): the POSTs below must
# carry X-Platform-Key matching TurnCall's PLATFORM_API_KEY, or TurnCall answers
# 401. Read it from the environment, falling back to the builder's .env.
PLATFORM_KEY="${PLATFORM_API_KEY:-}"
if [ -z "$PLATFORM_KEY" ] && [ -f .env ]; then
  PLATFORM_KEY="$(sed -n 's/^PLATFORM_API_KEY=//p' .env | head -n1)"
fi
if [ -z "$PLATFORM_KEY" ]; then
  echo "PLATFORM_API_KEY is not set (env or .env). It must match TurnCall's PLATFORM_API_KEY." >&2
  exit 1
fi

# Wait for TurnCall to be ready — it may still be booting after `make docker-up`.
printf "waiting for TurnCall at %s " "$BASE"
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "$BASE/health" 2>/dev/null; then ready=1; break; fi
  printf "."; sleep 1
done
echo
if [ "${ready:-}" != "1" ]; then
  echo "TurnCall not reachable at $BASE — is it up? (in turncall/: make docker-up)" >&2
  exit 1
fi

# Bootstrap calls carry X-Platform-Key. A 401 means the key doesn't match
# TurnCall's PLATFORM_API_KEY; any other non-2xx is reported with its status and
# body — either way, plainly, instead of a JSON traceback downstream.
bootstrap() {  # usage: bootstrap <url> [curl-args...]  -- prints the response body
  local url="$1"; shift
  local out status body
  out=$(curl -sS -w $'\n%{http_code}' -X POST "$url" -H "X-Platform-Key: $PLATFORM_KEY" "$@") \
    || out=$'\n000'
  status="${out##*$'\n'}"
  body="${out%$'\n'*}"
  case "$status" in
    2??) printf '%s' "$body" ;;
    401) echo "TurnCall rejected $url — X-Platform-Key doesn't match TurnCall's PLATFORM_API_KEY (401)." >&2
         exit 1 ;;
    000) echo "Request to $url failed before TurnCall answered (network/TLS)." >&2
         exit 1 ;;
    *)   echo "TurnCall rejected $url (HTTP $status): $body" >&2
         exit 1 ;;
  esac
}

# Capture each response before parsing it: piping bootstrap straight into python3
# runs both sides concurrently, so bootstrap's `exit 1` wouldn't stop python3 from
# printing a JSONDecodeError over the message above.
resp=$(bootstrap "$BASE/v1/projects" \
  -H 'Content-Type: application/json' -d '{"name":"builder"}')
PID=$(printf '%s' "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")

resp=$(bootstrap "$BASE/v1/api-keys?project_id=$PID" \
  -H 'Content-Type: application/json' -d '{"name":"builder","role":"admin"}')
KEY=$(printf '%s' "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['raw_key'])")

python3 - "$KEY" <<'PY'
import os, re, sys
key = sys.argv[1]
path = ".env"
src = open(path).read() if os.path.exists(path) else ""
if re.search(r'^TURNCALL_API_KEY=', src, flags=re.M):
    src = re.sub(r'^TURNCALL_API_KEY=.*$', f'TURNCALL_API_KEY={key}', src, flags=re.M)
else:
    src += f'\nTURNCALL_API_KEY={key}\n'
open(path, "w").write(src)
PY

echo "Wrote fresh TURNCALL_API_KEY to .env (project $PID). Restart: docker compose up -d"
