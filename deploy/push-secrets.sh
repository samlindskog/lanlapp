#!/usr/bin/env bash
# Write the bootstrap secrets file on the production host from environment variables set locally.
#   export WEB_PASSWORD='...'          # password for the jobs.slindskog.net login page
#   export OPENAI_API_KEY='sk-...'     # optional; leave unset/empty to skip semantic search
#   deploy/push-secrets.sh [ssh-host]  # default host: sldesktoplocal (the desktop over the LAN)
# The values travel only over ssh, are written 0600, and the bootstrap deletes the file after use.
set -euo pipefail
HOST="${1:-sldesktoplocal}"
: "${WEB_PASSWORD:?set WEB_PASSWORD in your environment first}"
printf 'WEB_PASSWORD=%s\nOPENAI_API_KEY=%s\n' "$WEB_PASSWORD" "${OPENAI_API_KEY:-}" \
  | ssh "$HOST" 'umask 077; mkdir -p ~/jobboard-bootstrap; cat > ~/jobboard-bootstrap/secrets.env && echo "secrets.env written on $(hostname) ($(wc -l < ~/jobboard-bootstrap/secrets.env) lines)"'
