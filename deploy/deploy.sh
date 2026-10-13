#!/usr/bin/env bash
# Deploy the current master (or a given ref) on the VPS. Runs as the `deploy` user, no sudo needed.
#   /opt/jobboard/deploy/deploy.sh [git-ref]      (default origin/master)
set -euo pipefail
REPO=/opt/jobboard
REF="${1:-origin/master}"
cd "$REPO"
echo "== fetching"
git fetch --quiet origin
git reset --hard --quiet "$REF"
echo "== at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
echo "== compose up"
# --force-recreate: the code is bind-mounted, so a new commit does not change the image; without
# recreating, the running process keeps the old modules it already imported.
docker compose -f jobboard/compose.prod.yaml --env-file .env up -d --build --remove-orphans --force-recreate jobboard
docker compose -f jobboard/compose.prod.yaml --env-file .env up -d --remove-orphans
echo "== health"
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login.html || true)
  if [ "$code" = "200" ]; then echo "ok (login page 200)"; break; fi
  sleep 2
  [ "$i" = 40 ] && { echo "app did not come up"; docker compose -f jobboard/compose.prod.yaml --env-file .env logs --tail 50 jobboard; exit 1; }
done
docker image prune -f >/dev/null 2>&1 || true
echo "== deployed"
