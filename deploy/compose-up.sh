#!/usr/bin/env bash
# Root helper: (re)build and start the production stack from /opt/jobboard, then health-check.
# Installed by bootstrap as /usr/local/sbin/jobboard-compose-up (root-owned, 0755) and allowed to the
# `deploy` user via sudoers without a password. Takes no arguments and reads nothing from the caller, so
# a compromised deploy key cannot make it do anything but deploy what is checked out.
set -euo pipefail
cd /opt/jobboard
C="docker compose -f jobboard/compose.prod.yaml --env-file .env"
# The app image is built from the checkout (code baked in), so --build picks up the new commit.
$C up -d --build --remove-orphans
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login.html || true)
  if [ "$code" = "200" ]; then echo "ok (login page 200)"; exit 0; fi
  sleep 2
done
echo "app did not come up"; $C logs --tail 50 jobboard; exit 1
