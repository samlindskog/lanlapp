#!/usr/bin/env bash
# One-time VPS bootstrap for jobboard. Run ONCE as root on uscentral1:
#   sudo bash ~/jobboard-bootstrap/bootstrap.sh
# Expects in the same directory: github_deploy_key (private, read-only GitHub deploy key),
# jobboard_deploy.pub (public key for the `deploy` user: used by Claude and GitHub Actions).
# Reads the web password and OpenAI key from secrets.env in the same directory
# (WEB_PASSWORD=..., OPENAI_API_KEY=...; deleted after use), or prompts if absent.
# Idempotent where practical.
#
# Does: remove hermes-hub (nginx site, nftables table, timers, /opt/hermes-hub, redis, slapd);
#       create `deploy` user; clone the repo to /opt/jobboard; write .env; nginx site for
#       jobs.slindskog.net + homepage; nftables input policy (ssh, wireguard, 443, MCP on wg0 only);
#       first deploy; issue Claude's API token.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_SSH="git@github.com:samlindskog/lanlapp.git"
APP=/opt/jobboard
WG_IF=wg0
WG_IP=10.88.0.1
MCP_PORT=8090
log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

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

log "remove hermes-hub"
for u in $(systemctl list-units --all --no-pager --plain 2>/dev/null | awk '{print $1}' | grep -Ei 'hermes|cloudflare-ips' || true); do
  systemctl disable --now "$u" 2>/dev/null || true; done
rm -f /etc/systemd/system/*hermes* /etc/systemd/system/*cloudflare-ips* 2>/dev/null || true
systemctl daemon-reload
rm -f /etc/nginx/sites-enabled/hermes-hub.conf /etc/nginx/sites-available/hermes-hub.conf /etc/nginx/conf.d/hermes-limits.conf
nft delete table inet hermes_hub 2>/dev/null || true
if [ -f /etc/nftables.conf ]; then sed -i '/hermes/d' /etc/nftables.conf; fi
rm -f /etc/nftables.d/hermes-*.nft 2>/dev/null || true
docker ps -aq --filter name=hermes | xargs -r docker rm -f >/dev/null 2>&1 || true
docker network ls -q --filter name=hermes | xargs -r docker network rm >/dev/null 2>&1 || true
docker volume ls -q --filter name=hermes | xargs -r docker volume rm >/dev/null 2>&1 || true
rm -rf /opt/hermes-hub /etc/sudoers.d/hermes
systemctl disable --now redis-server slapd 2>/dev/null || true
apt-get purge -y -qq redis-server slapd >/dev/null 2>&1 || true
apt-get autoremove -y -qq >/dev/null 2>&1 || true
echo "hermes-hub, redis, slapd removed"

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

log "api token for Claude"
if [ ! -f "$HERE/claude-token" ]; then
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
