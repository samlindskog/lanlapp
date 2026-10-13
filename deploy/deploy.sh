#!/usr/bin/env bash
# Deploy entry point for the `deploy` user. Installed by bootstrap as /usr/local/bin/jobboard-deploy
# (root-owned) and set as the FORCED COMMAND for every key in /home/deploy/.ssh/authorized_keys, so a
# holder of a deploy key can do exactly one thing: deploy a ref of the repo.
#   ssh deploy@vps                       -> deploys origin/master
#   ssh deploy@vps <sha-or-origin/ref>   -> deploys that ref (the forced command reads SSH_ORIGINAL_COMMAND)
# Runs the git steps as `deploy`, then hands the container work to root via a sudoers-whitelisted helper.
set -euo pipefail
REPO=/opt/jobboard
REF="${SSH_ORIGINAL_COMMAND:-${1:-origin/master}}"
REF="${REF##* }"                                   # tolerate "jobboard-deploy <ref>"
[[ "$REF" =~ ^(origin/[A-Za-z0-9._/-]+|[0-9a-f]{7,40})$ ]] || { echo "refusing ref: $REF"; exit 2; }
cd "$REPO"
echo "== fetching"
git fetch --quiet origin
git reset --hard --quiet "$REF"
echo "== at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
echo "== compose up (as root via sudo helper)"
sudo -n /usr/local/sbin/jobboard-compose-up
echo "== deployed"
