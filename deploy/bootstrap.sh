#!/usr/bin/env bash
# One-time VPS bootstrap for jobboard. Run ONCE as root on uscentral1:
#   sudo bash ~/jobboard-bootstrap/bootstrap.sh
# Expects in the same directory: github_deploy_key (private, read-only GitHub deploy key),
# jobboard_deploy.pub (public key for the `deploy` user: used by Claude and GitHub Actions).
# Reads the web password and OpenAI key from secrets.env in the same directory
# (WEB_PASSWORD=..., OPENAI_API_KEY=...; deleted after use), or prompts if absent.
# Idempotent where practical.
#
# Does: create `deploy` user; clone the repo to /opt/jobboard; write .env; nginx site for
#       jobs.slindskog.net + homepage; nftables input policy (ssh, wireguard, 443, MCP on wg0 only);
#       first deploy; restore a staged dump into an empty database; issue Claude's API token.
# Maintenance modes (flag file in the same directory, then run): show-firewall, reboot, remove-sudo-rule.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_SSH="git@github.com:samlindskog/lanlapp.git"
APP=/opt/jobboard
WG_IF=wg0
WG_IP=10.88.0.1
MCP_PORT=8090
log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# --- maintenance mode: `touch ~/jobboard-bootstrap/remove-sudo-rule` then run; deletes the sudoers
# rule that allows this script to run without a password. After this, root needs a password again.
if [ -f "$HERE/remove-sudo-rule" ]; then
  rm -f "$HERE/remove-sudo-rule" /etc/sudoers.d/jobboard-bootstrap
  visudo -c >/dev/null && echo "sudoers rule removed; remaining sudoers.d: $(ls /etc/sudoers.d | tr '\n' ' ')"
  exit 0
fi

# --- maintenance mode: `touch ~/jobboard-bootstrap/show-firewall` then run; prints the live nftables
# ruleset and listening sockets, changes nothing.
if [ -f "$HERE/show-firewall" ]; then
  rm -f "$HERE/show-firewall"
  echo "===== nft list ruleset"; nft list ruleset
  echo "===== listening sockets"; ss -tulnp | awk 'NR==1 || /LISTEN|UNCONN/'
  echo "===== wireguard"; wg show
  exit 0
fi

# --- maintenance mode: `touch ~/jobboard-bootstrap/reboot` then run; reboots the VPS (nothing else).
if [ -f "$HERE/reboot" ]; then
  rm -f "$HERE/reboot"
  echo "rebooting $(hostname) now ($(date -u +%FT%TZ)); kernel $(uname -r) -> $(ls /boot/vmlinuz-* | sed 's|.*/vmlinuz-||' | sort -V | tail -1)"
  systemctl reboot
  exit 0
fi

[ -f "$HERE/github_deploy_key" ] || { echo "missing $HERE/github_deploy_key"; exit 1; }
[ -f "$HERE/jobboard_deploy.pub" ] || { echo "missing $HERE/jobboard_deploy.pub"; exit 1; }

log "backups"
mkdir -p /root/jobboard-bootstrap-backup
nft list ruleset > /root/jobboard-bootstrap-backup/nft-ruleset-$(date +%s).nft 2>/dev/null || true
cp -a /etc/nftables.conf /root/jobboard-bootstrap-backup/ 2>/dev/null || true
cp -a /etc/nginx /root/jobboard-bootstrap-backup/nginx-$(date +%s) 2>/dev/null || true
echo "saved under /root/jobboard-bootstrap-backup (restore firewall: nft -f <file>)"

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
dpkg --configure -a 2>&1 | tail -3 || true
apt-get -f install -y -qq >/dev/null 2>&1 || true
if ! apt-get install -y -qq git nginx nftables curl >/dev/null 2>&1; then
  echo "apt-get install failed; continuing with the tools already present:"
fi
for t in git nginx nft curl docker; do command -v "$t" >/dev/null || { echo "missing required tool: $t"; exit 1; }; done
echo "git nginx nft curl docker present"

log "deploy user"
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy "$HERE/jobboard_deploy.pub" /home/deploy/.ssh/authorized_keys
install -m 600 -o deploy -g deploy "$HERE/github_deploy_key" /home/deploy/.ssh/github_deploy_key
cat > /home/deploy/.ssh/config <<EOF
Host github.com
  IdentityFile /home/deploy/.ssh/github_deploy_key
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chown deploy:deploy /home/deploy/.ssh/config; chmod 600 /home/deploy/.ssh/config
chmod 750 /home/deploy
# sshd may restrict logins to listed users/groups: add deploy if so.
SSHD_CHANGED=0
for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do
  [ -f "$f" ] || continue
  if grep -qE '^AllowUsers' "$f" && ! grep -qE '^AllowUsers.*\bdeploy\b' "$f"; then
    sed -i -E 's/^(AllowUsers.*)$/\1 deploy/' "$f"; SSHD_CHANGED=1; fi
  if grep -qE '^AllowGroups' "$f" && ! grep -qE '^AllowGroups.*\bdeploy\b' "$f"; then
    sed -i -E 's/^(AllowGroups.*)$/\1 deploy/' "$f"; SSHD_CHANGED=1; fi
done
if [ "$SSHD_CHANGED" = 1 ]; then sshd -t && systemctl reload ssh && echo "sshd: deploy added to AllowUsers/AllowGroups"; fi
# Unlock the account for key-based login (useradd leaves '!' in the shadow field; some PAM/sshd
# setups treat that as locked) and show what sshd will do for deploy.
usermod -p '*' deploy 2>/dev/null || true
echo "sshd for deploy: $(sshd -T -C user=deploy,host=x,addr=203.0.113.1 2>/dev/null | grep -E '^(allowusers|pubkeyauthentication|strictmodes|authorizedkeysfile)' | tr '\n' ' ')"
echo "authorized_keys: $(ssh-keygen -lf /home/deploy/.ssh/authorized_keys 2>&1)"
echo "shadow: $(getent shadow deploy | cut -d: -f2 | cut -c1-2)…  home: $(stat -c '%A %U' /home/deploy) .ssh: $(stat -c '%A %U' /home/deploy/.ssh)"
echo "recent sshd lines for deploy:"; journalctl -u ssh --since "30 min ago" --no-pager 2>/dev/null | grep -i deploy | tail -4 || true

log "checkout $APP"
if [ ! -d "$APP/.git" ]; then
  install -d -o deploy -g deploy "$APP"
  su - deploy -c "git clone --quiet $REPO_SSH $APP"
else
  su - deploy -c "cd $APP && git fetch --quiet origin && git reset --hard --quiet origin/master"
fi
chmod +x "$APP/deploy/deploy.sh"

log "secrets -> $APP/.env"
if [ ! -f "$APP/.env" ]; then
  if [ -f "$HERE/secrets.env" ]; then
    # shellcheck disable=SC1090
    . "$HERE/secrets.env"
    PW1="${WEB_PASSWORD:?WEB_PASSWORD missing in secrets.env}"; OAI="${OPENAI_API_KEY:-}"
    shred -u "$HERE/secrets.env" 2>/dev/null || rm -f "$HERE/secrets.env"
    echo "secrets read from secrets.env (file removed)"
  else
    read -rsp "web password for jobs.slindskog.net: " PW1; echo
    read -rsp "again: " PW2; echo
    [ "$PW1" = "$PW2" ] || { echo "passwords differ"; exit 1; }
    read -rsp "OpenAI API key (blank to skip semantic search): " OAI; echo
  fi
  HASH=$(cd "$APP/jobboard" && python3 -c "import auth,sys; print(auth.hash_password(sys.stdin.readline().rstrip('\n')))" <<<"$PW1")
  umask 077
  # compose interpolates $VAR in env files, so every literal $ in the hash must be written as $$.
  cat > "$APP/.env" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JOBBOARD_PASSWORD_HASH=${HASH//\$/\$\$}
JOBBOARD_SECRET=$(openssl rand -hex 32)
OPENAI_API_KEY=$OAI
EOF
  chown deploy:deploy "$APP/.env"; chmod 600 "$APP/.env"
  echo "written"
else
  # Repair an .env written before $-escaping: double any single $ on the hash line.
  if grep -qE '^JOBBOARD_PASSWORD_HASH=.*[^$]\$[^$]' "$APP/.env"; then
    sed -i -E '/^JOBBOARD_PASSWORD_HASH=/ s/\$/$$/g; /^JOBBOARD_PASSWORD_HASH=/ s/\$\$\$\$/$$/g' "$APP/.env"
    echo "exists; escaped \$ in the password hash for compose"
  else
    echo "exists, kept"
  fi
fi

log "nginx"
install -d -m 755 /var/www/slindskog.net
install -m 644 "$APP/deploy/homepage.html" /var/www/slindskog.net/index.html
install -m 644 "$APP/deploy/nginx-jobboard.conf" /etc/nginx/sites-available/jobboard.conf
ln -sf /etc/nginx/sites-available/jobboard.conf /etc/nginx/sites-enabled/jobboard.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "nginx reloaded: jobs.slindskog.net -> 127.0.0.1:8080, homepage, default 444"

log "firewall (nftables, input policy only; NAT/forward left as they are)"
WG_PORT=$(wg show "$WG_IF" listen-port 2>/dev/null || echo 51820)
install -d /etc/nftables.d
cat > /etc/nftables.d/jobboard.nft <<EOF
# jobboard input policy — installed by deploy/bootstrap.sh
table inet jobboard {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ct state invalid drop
    ip protocol icmp accept
    ip6 nexthdr icmpv6 accept
    tcp dport 22 accept                       comment "ssh"
    udp dport $WG_PORT accept                 comment "wireguard"
    tcp dport 443 accept                      comment "https (cloudflare origin pull)"
    iifname "$WG_IF" tcp dport $MCP_PORT accept comment "mcp over wireguard only"
    iifname "$WG_IF" udp dport 53 accept
  }
}
EOF
grep -q 'nftables.d/jobboard.nft' /etc/nftables.conf 2>/dev/null || echo 'include "/etc/nftables.d/jobboard.nft"' >> /etc/nftables.conf
grep -q '^#!/usr/sbin/nft -f' /etc/nftables.conf || sed -i '1i #!/usr/sbin/nft -f' /etc/nftables.conf
nft -c -f /etc/nftables.conf
nft delete table inet jobboard 2>/dev/null || true
nft -f /etc/nftables.d/jobboard.nft
systemctl enable nftables >/dev/null 2>&1 || true
echo "input: ssh 22, wireguard udp $WG_PORT, https 443, mcp $MCP_PORT on $WG_IF only. Everything else dropped."

log "first deploy"
su - deploy -c "$APP/deploy/deploy.sh"

log "database restore (if a dump is staged and the jobs table is empty)"
DUMP=/tmp/jobscan-dump.sql.gz
if [ -f "$DUMP" ]; then
  N=$(su - deploy -c "cd $APP && docker compose -f jobboard/compose.prod.yaml --env-file .env exec -T db psql -U jobscan -d jobscan -tAc 'SELECT COUNT(*) FROM jobs'" 2>/dev/null | tail -1 || echo "?")
  if [ "$N" = "0" ]; then
    su - deploy -c "cd $APP && gunzip -c $DUMP | docker compose -f jobboard/compose.prod.yaml --env-file .env exec -T db psql -U jobscan -d jobscan -q -v ON_ERROR_STOP=0" >/tmp/jobscan-restore.log 2>&1 || true
    N2=$(su - deploy -c "cd $APP && docker compose -f jobboard/compose.prod.yaml --env-file .env exec -T db psql -U jobscan -d jobscan -tAc 'SELECT COUNT(*) FROM jobs'" 2>/dev/null | tail -1)
    echo "restored: jobs=$N2 (log: /tmp/jobscan-restore.log, errors: $(grep -c ERROR /tmp/jobscan-restore.log || true))"
    rm -f "$DUMP"
  else
    echo "jobs table has $N rows; dump left untouched"
  fi
else
  echo "no dump staged at $DUMP"
fi

log "api token for Claude"
HAVE=$(su - deploy -c "cd $APP && docker compose -f jobboard/compose.prod.yaml --env-file .env exec -T db psql -U jobscan -d jobscan -tAc \"SELECT COUNT(*) FROM auth_tokens WHERE name='claude-mac' AND revoked_at IS NULL\"" 2>/dev/null | tail -1 || echo 0)
if [ "${HAVE:-0}" != "0" ]; then
  echo "an active claude-mac token already exists; not minting another (revoke it via the API to re-mint)"
elif [ ! -f "$HERE/claude-token" ]; then
  TOK=$(su - deploy -c "cd $APP && docker compose -f jobboard/compose.prod.yaml --env-file .env exec -T jobboard python3 server.py issue-token claude-mac" 2>/dev/null | tail -1)
  if [ -n "$TOK" ]; then
    install -m 600 -o "$(stat -c %U "$HERE")" /dev/null "$HERE/claude-token"
    echo "$TOK" > "$HERE/claude-token"
    echo "token written to $HERE/claude-token (delete after copying)"
  else
    echo "token issuance failed; later: docker compose ... exec jobboard python3 server.py issue-token <name>"
  fi
else
  echo "exists, kept"
fi

log "done"
echo "DNS: add an A record  jobs.slindskog.net -> $(curl -s -4 ifconfig.me)  (proxied / orange cloud) in Cloudflare."
echo "MCP: http://$WG_IP:$MCP_PORT/mcp over WireGuard, Authorization: Bearer <token>."
