#!/usr/bin/env bash
# Root helper: (re)build and start the production stack from /opt/jobboard, then health-check.
# Installed by bootstrap as /usr/local/sbin/jobboard-compose-up (root-owned, 0755) and allowed to the
# `deploy` user via sudoers without a password. Takes no arguments and reads nothing from the caller, so
# a compromised deploy key cannot make it do anything but deploy what is checked out.
set -euo pipefail
cd /opt/jobboard
C="docker compose -f jobboard/compose.prod.yaml --env-file .env"
# The app joins the external network `crawl` (the crawl stack's); compose refuses to start without it. The subnet
# is fixed because the app's relay trusts it (JOBSCAN_RELAY_PEERS in compose.prod.yaml); a network that already
# exists with another subnet is kept, with a warning.
CRAWL_SUBNET=172.31.0.0/24
if docker network inspect crawl >/dev/null 2>&1; then
  have=$(docker network inspect crawl -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
  [ "$have" = "$CRAWL_SUBNET" ] || echo "WARNING: network crawl has subnet ${have:-?}, not $CRAWL_SUBNET; kept (JOBSCAN_RELAY_PEERS must match)"
else
  docker network create --subnet "$CRAWL_SUBNET" crawl >/dev/null
fi
# The app image is built from the checkout (code baked in), so --build picks up the new commit.
$C up -d --build --remove-orphans
for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login.html || true)
  if [ "$code" = "200" ]; then echo "ok (login page 200)"; exit 0; fi
  sleep 2
done
echo "app did not come up"; $C logs --tail 50 jobboard; exit 1
