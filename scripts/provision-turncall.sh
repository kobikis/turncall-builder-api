#!/usr/bin/env bash
# Provision a TurnCall project + admin API key and write it into .env.
#
# TurnCall's local DB is wiped on `make docker-down` (--volumes), which
# invalidates the builder's key (401 on create). Run this after (re)starting
# TurnCall to mint a fresh key. Idempotent enough — it just creates a new
# project+key each run and rewrites the TURNCALL_API_KEY line.
set -euo pipefail

BASE="${TURNCALL_HOST_URL:-http://localhost:8090}"   # TurnCall API as seen from the host

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

PID=$(curl -fsS -X POST "$BASE/v1/projects" \
  -H 'Content-Type: application/json' -d '{"name":"builder"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")

KEY=$(curl -fsS -X POST "$BASE/v1/api-keys?project_id=$PID" \
  -H 'Content-Type: application/json' -d '{"name":"builder","role":"admin"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['raw_key'])")

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
