#!/usr/bin/env bash
# Deploy entry point for the `deploy` user. Installed by bootstrap as /usr/local/bin/jobboard-deploy
# (root-owned) and set as the FORCED COMMAND for the deploy user, twice: in sshd's Match block for the
# user and on every key in /home/deploy/.ssh/authorized_keys. A holder of a deploy key can therefore do
# exactly one thing: deploy origin/master.
#   over ssh (GitHub Actions)   whatever is sent is ignored: origin/master is deployed
#   locally, by root/bootstrap  jobboard-deploy [sha-or-origin/ref]
# Signed commits: when /etc/jobboard/allowed_signers exists (staged as `allowed_signers` for the bootstrap:
# an ssh allowed-signers file, one "<principal> <key type> <key>" line per signing key), the commit must
# carry an SSH signature by one of those keys (git verify-commit) or nothing is deployed, so neither a leaked
# deploy key nor a hijacked repository can put unsigned code on this host. Without the file every commit
# of the repository deploys (a line says so).
# Runs the git steps as `deploy`, then hands the container work to root via a sudoers-whitelisted helper.
set -euo pipefail
REPO=/opt/jobboard
SIGNERS=/etc/jobboard/allowed_signers
if [ -n "${SSH_CONNECTION:-}" ]; then
  REF=origin/master                                # a key holder chooses nothing
else
  REF="${1:-origin/master}"
  [[ "$REF" =~ ^(origin/[A-Za-z0-9._/-]+|[0-9a-f]{7,40})$ ]] || { echo "refusing ref: $REF"; exit 2; }
fi
cd "$REPO"
echo "== fetching"
git fetch --quiet origin
if [ -f "$SIGNERS" ]; then
  if git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-commit "$REF" >/dev/null 2>&1; then
    echo "== $REF: signature ok ($(git log -1 --pretty='%GS' "$REF"))"
  else
    echo "refusing $REF: no valid SSH signature by a key listed in $SIGNERS"; exit 3
  fi
else
  echo "== (no $SIGNERS: commits are not signature-checked)"
fi
git reset --hard --quiet "$REF"
echo "== at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
echo "== compose up (as root via sudo helper)"
sudo -n /usr/local/sbin/jobboard-compose-up
echo "== deployed"
