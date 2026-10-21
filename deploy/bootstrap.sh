#!/usr/bin/env bash
# Desktop bootstrap for jobboard (sldesktop, Debian 13, behind the WireGuard hub uscentral1). Run as root:
#   sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh
# Stage beside it (first run; re-runs reuse the installed copies when a staged file is absent):
#   github_deploy_key              private read-only GitHub deploy key (clones the repo as `deploy`)
#   jobboard_deploy.pub            public key(s) for the `deploy` user: Claude and GitHub Actions
#   origin.pem origin.key          Cloudflare Origin CA certificate + key (*.slindskog.net)
#   cloudflare-origin-pull-ca.pem  Cloudflare's Authenticated Origin Pulls CA
#   secrets.env                    WEB_PASSWORD=..., OPENAI_API_KEY=... (deploy/push-secrets.sh); deleted after use
#     or jobboard.env              a complete .env exported from the old host (export-secrets mode): adopted verbatim
#   jobscan-dump.sql.gz            optional: restored into an EMPTY database on the full run (dump mode on the old host)
#   deploy-ref                     optional: one line, the ref to check out and deploy instead of origin/master
#                                  (origin/next before cutover); delete it once that branch has merged
# Idempotent where practical.
#
# Does: require docker; create `deploy` user; clone the repo to /opt/jobboard at DEPLOY_REF; write or adopt
#       .env; nginx on the WireGuard address only (jobs.slindskog.net + homepage, TLS from the staged certs,
#       MCP on WG_IP:MCP_PORT); nftables filter for packets arriving on wg0 ONLY (policy accept elsewhere,
#       Docker's tables untouched); WireGuard watchdog; first deploy; crawl stack; restore a staged dump into
#       an empty database; issue Claude's API token.
# Maintenance modes (flag file in the same directory, then run; they run before any host work, so this
# script can be staged and run on the OLD host too):
#   show-firewall   print the live nftables ruleset, listening sockets and wg state; change nothing
#   dump            pg_dump the database to <this dir>/jobscan-dump.sql.gz (0600, staging owner): the backup path
#   export-secrets  copy /opt/jobboard/.env to <this dir>/jobboard.env (0600, staging owner)
#   recreate        app stack down and up again (fresh containers, same data)
#   export-certs    copy the installed TLS files (paths read from the live nginx config) and the deploy user's
#                   key files to <this dir> under the staging names above (0600, staging owner)
#   restore         REPLACE the database from <this dir>/jobscan-dump.sql.gz (stop app, drop, recreate, restore, start)
#   egress          route every container's internet traffic through the hub (wg0.conf: Table off, AllowedIPs
#                   0.0.0.0/0, table 100; policy rules + kill switch via jobboard-egress.service); verifies, else reverts
#   egress-off      remove the policy, restore the pre-egress wg0.conf, restart the tunnel
#   reboot          reboot the host
#   remove-sudo-rule remove /etc/sudoers.d/jobboard-bootstrap
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
OWNER="$(stat -c %U "$HERE")"              # the staging directory's owner receives dumps / exports / tokens
REPO_SSH="git@github.com:samlindskog/lanlapp.git"
APP=/opt/jobboard

# --- parameters -------------------------------------------------------------------------------------
SSH_PORT=1025                              # this host's sshd (the hub forwards public 2222 here)
WG_IF=wg0
WG_IP=10.88.0.2                            # this host on the tunnel: nginx binds here and nowhere else
HUB_IP=10.88.0.1                           # uscentral1: source of every DNAT'd (masqueraded) public flow
LAPTOP_IP=10.88.0.3                        # the Mac: ssh and MCP, routed (not NATed) through the hub
MCP_PORT=8090
DEPLOY_REF=origin/master                   # what the checkout and the first deploy track; GitHub Actions deploys origin/master
CRAWL=1                                    # 1: also run deploy/crawl/compose.yaml (Firecrawl + SearXNG) as project `crawl`
CRAWL_SUBNET=172.31.0.0/24                 # fixed, so the app's relay can trust the crawl containers (JOBSCAN_RELAY_PEERS)
CONTAINER_NETS=172.31.0.0/23               # every bridge of ours: crawl 172.31.0.0/24 + the app stack 172.31.1.0/24 (compose.prod.yaml)
HUB_PUBLIC_IP=72.14.178.40                 # the exit address containers show once egress goes through the hub
WG_NET="${WG_IP%.*}.0/24"
# egress mode (below): where its pieces live
EGRESS_BIN=/usr/local/sbin/jobboard-egress
EGRESS_UNIT=jobboard-egress.service
EGRESS_SYSCTL=/etc/sysctl.d/90-jobboard-egress.conf
WG_CONF=/etc/wireguard/$WG_IF.conf
WG_ORIG=/root/jobboard-bootstrap-backup/$WG_IF.conf.pre-egress      # the config before egress was ever applied; egress-off restores it
WG_DROPIN=/etc/systemd/system/wg-quick@$WG_IF.service.d/jobboard-egress.conf
DOCKER_DROPIN=/etc/systemd/system/docker.service.d/jobboard-egress.conf   # docker waits for the rules: no LAN leak at boot
# A staged `deploy-ref` file overrides the ref (one line, e.g. origin/next before cutover). Delete it once the
# branch has merged, or a re-run of this script for an nginx/crawl change would reset the checkout to it.
if [ -f "$HERE/deploy-ref" ]; then
  DEPLOY_REF=$(head -1 "$HERE/deploy-ref" | tr -d '[:space:]')
  [[ "$DEPLOY_REF" =~ ^(origin/[A-Za-z0-9._/-]+|[0-9a-f]{7,40})$ ]] || { echo "refusing ref in $HERE/deploy-ref: $DEPLOY_REF"; exit 1; }
fi
log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
# Every compose call for the production stack; the deploy helper uses the same files (deploy/compose-up.sh).
C() { (cd "$APP" && docker compose -f jobboard/compose.prod.yaml --env-file .env "$@"); }
psql_db() { C exec -T db psql -U jobscan -d "${1:-jobscan}" -tAq "${@:2}"; }
# The crawl network has a fixed subnet (the relay trusts it); an existing network with another subnet is kept, with a warning.
ensure_crawl_network() {
  if docker network inspect crawl >/dev/null 2>&1; then
    local have; have=$(docker network inspect crawl -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
    [ "$have" = "$CRAWL_SUBNET" ] || echo "WARNING: network crawl has subnet ${have:-?}, not $CRAWL_SUBNET; kept (JOBSCAN_RELAY_PEERS in compose.prod.yaml must match)"
  else
    docker network create --subnet "$CRAWL_SUBNET" crawl >/dev/null && echo "network crawl created ($CRAWL_SUBNET)"
  fi
}
wg_restart_and_wait() {   # restart the tunnel, then give the hub 20 s to answer over it
  systemctl restart "wg-quick@$WG_IF"
  for _ in $(seq 1 20); do ping -q -c 1 -W 1 -I "$WG_IF" "$HUB_IP" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
exit_ip_check() {   # what the internet sees as the containers' address; through the app container, else a curl image on `crawl`
  local seen
  if [ -n "$(docker ps -q -f name='^jobboard$' 2>/dev/null)" ]; then
    seen=$(docker exec jobboard python3 -c 'import urllib.request;print(urllib.request.urlopen("https://api.ipify.org",timeout=20).read().decode().strip())' 2>&1 | tail -1 || true)
  else
    seen=$(docker run --rm --network crawl curlimages/curl -s --max-time 20 https://api.ipify.org 2>&1 | tail -1 || true)
  fi
  if [ "$seen" = "$HUB_PUBLIC_IP" ]; then echo "exit address from a container: $seen = the hub. Egress works."
  else echo "WARNING: exit address from a container: '${seen:-<no answer>}' (expected $HUB_PUBLIC_IP). Check: $EGRESS_BIN status; ip route show table 100; wg show"; return 1; fi
}
container_nets_ok() {   # print the subnets of our two networks; return 1 if any lies outside CONTAINER_NETS (or is missing)
  local rc=0 net subs
  for net in jobboard_default crawl; do
    subs=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$net" 2>/dev/null || true)
    [ -n "$subs" ] || { echo "  $net: no such network"; rc=1; continue; }
    # shellcheck disable=SC2086  # subs is a word list of CIDRs
    if python3 -c 'import ipaddress,sys; o=ipaddress.ip_network(sys.argv[1]); sys.exit(0 if all(ipaddress.ip_network(s).subnet_of(o) for s in sys.argv[2:]) else 1)' "$CONTAINER_NETS" $subs; then
      echo "  $net: $subs(inside $CONTAINER_NETS)"
    else
      echo "  $net: ${subs}OUTSIDE $CONTAINER_NETS: its containers would NOT match the egress rules"; rc=1
    fi
  done
  return $rc
}
wait_app() {
  for _ in $(seq 1 40); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login.html || true)" = 200 ] && return 0
    sleep 2
  done
  echo "app did not come up"; C logs --tail 50 jobboard; return 1
}

# --- maintenance mode: `touch <dir>/remove-sudo-rule` then run; deletes the sudoers rule that allows this
# script to run without a password. After this, root needs a password again.
if [ -f "$HERE/remove-sudo-rule" ]; then
  rm -f "$HERE/remove-sudo-rule" /etc/sudoers.d/jobboard-bootstrap
  visudo -c >/dev/null && echo "sudoers rule removed; remaining sudoers.d: $(ls /etc/sudoers.d | tr '\n' ' ')"
  exit 0
fi

# --- maintenance mode: `touch <dir>/show-firewall` then run; prints the live nftables ruleset, listening
# sockets and WireGuard state, changes nothing. Run this on BOTH hosts before any firewall work.
if [ -f "$HERE/show-firewall" ]; then
  rm -f "$HERE/show-firewall"
  echo "===== nft list ruleset"; nft list ruleset
  echo "===== /etc/nftables.conf"; cat /etc/nftables.conf 2>/dev/null || echo "(none)"
  echo "===== /etc/nftables.d"; ls -l /etc/nftables.d 2>/dev/null || echo "(none)"
  echo "===== listening sockets"; ss -tulnp | awk 'NR==1 || /LISTEN|UNCONN/'
  echo "===== wireguard"; wg show; ls /etc/wireguard 2>/dev/null
  echo "===== nginx sites-enabled"; ls -l /etc/nginx/sites-enabled 2>/dev/null || echo "(none)"
  echo "===== sysctl"; sysctl net.ipv4.ip_forward net.ipv4.ip_nonlocal_bind 2>/dev/null
  exit 0
fi

# --- maintenance mode: `touch <dir>/reboot` then run; reboots the host (nothing else).
if [ -f "$HERE/reboot" ]; then
  rm -f "$HERE/reboot"
  echo "rebooting $(hostname) now ($(date -u +%FT%TZ)); kernel $(uname -r) -> $(ls /boot/vmlinuz-* | sed 's|.*/vmlinuz-||' | sort -V | tail -1)"
  systemctl reboot
  exit 0
fi

# --- maintenance mode: `touch <dir>/dump` then run; pg_dump through the running db container to
# <dir>/jobscan-dump.sql.gz, readable by the staging owner only. Also the off-box backup path: scp it away.
if [ -f "$HERE/dump" ]; then
  rm -f "$HERE/dump"
  [ -f "$APP/.env" ] || { echo "no $APP/.env: nothing to dump here"; exit 1; }
  OUT="$HERE/jobscan-dump.sql.gz"
  install -m 600 -o "$OWNER" -g "$(id -gn "$OWNER")" /dev/null "$OUT"
  C exec -T db pg_dump -U jobscan jobscan | gzip > "$OUT"
  echo "dumped: $(stat -c '%s bytes %U %a' "$OUT") $OUT  (jobs=$(psql_db jobscan -c 'SELECT COUNT(*) FROM jobs' | tail -1))"
  echo "fetch it with: scp $(hostname -s):$OUT ."
  exit 0
fi

# --- maintenance mode: `touch <dir>/export-secrets` then run; copies the stack's .env to <dir>/jobboard.env
# (0600, staging owner) for adoption by the bootstrap on another host. Delete the copy after moving it.
if [ -f "$HERE/export-secrets" ]; then
  rm -f "$HERE/export-secrets"
  [ -f "$APP/.env" ] || { echo "no $APP/.env to export"; exit 1; }
  install -m 600 -o "$OWNER" -g "$(id -gn "$OWNER")" "$APP/.env" "$HERE/jobboard.env"
  echo "exported $(wc -l < "$HERE/jobboard.env") lines to $HERE/jobboard.env ($(cut -d= -f1 "$HERE/jobboard.env" | tr '\n' ' '))"
  exit 0
fi

# --- maintenance mode: `touch <dir>/export-certs` then run; copies what the NEW host's first run needs and
# the old host already has: the Cloudflare origin cert, key and origin-pull CA (paths taken from whichever
# nginx TLS snippet is installed here) and the deploy user's GitHub key + authorized public keys. Written
# 0600 to the staging owner under the staging names; scp them off and delete them here afterwards.
if [ -f "$HERE/export-certs" ]; then
  rm -f "$HERE/export-certs"
  TLS=; for f in /etc/nginx/jobboard/jobboard-tls.conf /etc/nginx/snippets/tls.conf; do [ -f "$f" ] && { TLS=$f; break; }; done
  [ -n "$TLS" ] || { echo "no nginx TLS snippet found (/etc/nginx/jobboard/jobboard-tls.conf or /etc/nginx/snippets/tls.conf)"; exit 1; }
  GRP=$(id -gn "$OWNER")
  for pair in ssl_certificate:origin.pem ssl_certificate_key:origin.key ssl_client_certificate:cloudflare-origin-pull-ca.pem; do
    SRC=$(sed -nE "s/^[[:space:]]*${pair%%:*}[[:space:]]+([^;[:space:]]+);.*/\1/p" "$TLS" | head -1)
    [ -n "$SRC" ] && [ -f "$SRC" ] || { echo "${pair%%:*}: no readable path in $TLS"; exit 1; }
    install -m 600 -o "$OWNER" -g "$GRP" "$SRC" "$HERE/${pair#*:}"; echo "  $SRC -> $HERE/${pair#*:}"
  done
  for pair in github_deploy_key:github_deploy_key deploy_keys.pub:jobboard_deploy.pub; do
    SRC=/home/deploy/.ssh/${pair%%:*}
    [ -f "$SRC" ] && install -m 600 -o "$OWNER" -g "$GRP" "$SRC" "$HERE/${pair#*:}" && echo "  $SRC -> $HERE/${pair#*:}" || echo "  $SRC absent; skipped"
  done
  echo "fetch with: scp $(hostname -s):$HERE/{origin.pem,origin.key,cloudflare-origin-pull-ca.pem,github_deploy_key,jobboard_deploy.pub} <dir>/   then delete them here"
  exit 0
fi

# --- maintenance mode: `touch <dir>/recreate` then run; takes the app stack down and up again so every
# container is created afresh (a network change can leave a reconnected container without its compose
# aliases: `db` stops resolving). Data stays in the volume. The crawl stack is untouched.
if [ -f "$HERE/recreate" ]; then
  rm -f "$HERE/recreate"
  [ -f "$APP/.env" ] || { echo "no $APP/.env: run the full bootstrap first"; exit 1; }
  C down --remove-orphans
  /usr/local/sbin/jobboard-compose-up
  exit 0
fi

# --- maintenance mode: `touch <dir>/restore` then run; REPLACES the database with <dir>/jobscan-dump.sql.gz:
# stop the app, drop and recreate `jobscan`, restore, start, health check, remove the dump. The stack must be
# deployed already (a full run). The dump stays in place if the restore fails.
if [ -f "$HERE/restore" ]; then
  rm -f "$HERE/restore"
  DUMP="$HERE/jobscan-dump.sql.gz"
  [ -f "$DUMP" ] || { echo "missing $DUMP (dump mode on the old host, then copy it here)"; exit 1; }
  [ -f "$APP/.env" ] || { echo "no $APP/.env: run the full bootstrap first"; exit 1; }
  gzip -t "$DUMP" || { echo "$DUMP is not a valid gzip file"; exit 1; }
  echo "before: jobs=$(psql_db jobscan -c 'SELECT COUNT(*) FROM jobs' 2>/dev/null | tail -1 || echo '?')"
  C stop jobboard
  # From here on the app stays stopped unless every step succeeds: a half-restored database must not serve.
  restore_failed() {
    echo "RESTORE FAILED ($1): app left stopped, database as the restore got it, dump kept at $DUMP."
    [ -f /tmp/jobscan-restore.log ] && { echo "last lines of /tmp/jobscan-restore.log:"; tail -20 /tmp/jobscan-restore.log; }
    echo "fix and re-run restore (touch $HERE/restore), or start the app by hand with:"
    echo "  cd $APP && docker compose -f jobboard/compose.prod.yaml --env-file .env start jobboard"
    exit 1
  }
  rm -f /tmp/jobscan-restore.log
  # jobscan is the container's superuser; connect to the maintenance db to drop its own.
  psql_db postgres -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS jobscan WITH (FORCE)' -c 'CREATE DATABASE jobscan OWNER jobscan' \
    || restore_failed "drop/create database"
  gunzip -c "$DUMP" | C exec -T db psql -U jobscan -d jobscan -q -v ON_ERROR_STOP=1 >/tmp/jobscan-restore.log 2>&1 \
    || restore_failed "psql restore"
  C start jobboard || restore_failed "container start"
  wait_app || restore_failed "health check"
  echo "restored: jobs=$(psql_db jobscan -c 'SELECT COUNT(*) FROM jobs' | tail -1) tokens=$(psql_db jobscan -c 'SELECT COUNT(*) FROM auth_tokens WHERE revoked_at IS NULL' | tail -1) (log: /tmp/jobscan-restore.log)"
  rm -f "$DUMP"
  exit 0
fi

# --- maintenance mode: `touch <dir>/egress-off` then run; removes the egress policy (rules, unit, drop-in,
# sysctl), restores the wg0.conf saved before egress was first applied, restarts the tunnel. Containers are
# back on the LAN default route.
if [ -f "$HERE/egress-off" ]; then
  rm -f "$HERE/egress-off"
  log "egress-off: containers back on the host's LAN route"
  systemctl disable --now "$EGRESS_UNIT" 2>/dev/null || true          # ExecStop removes the ip rules
  [ -x "$EGRESS_BIN" ] && { "$EGRESS_BIN" remove || true; }
  rm -f "/etc/systemd/system/$EGRESS_UNIT" "$WG_DROPIN" "$DOCKER_DROPIN" "$EGRESS_BIN" "$EGRESS_SYSCTL"
  systemctl daemon-reload                                               # docker no longer waits for the unit
  sysctl -q --system >/dev/null 2>&1 || true                           # rp_filter back to what the system's own files say
  if [ -f "$WG_ORIG" ]; then
    install -m 600 -o root -g root "$WG_ORIG" "$WG_CONF" && rm -f "$WG_ORIG"
    echo "$WG_CONF restored from the pre-egress copy (PersistentKeepalive and the peer as they were)"
  else
    echo "no pre-egress copy at $WG_ORIG: $WG_CONF left as it is (Table = off / AllowedIPs 0.0.0.0/0 may remain; harmless)"
  fi
  ip -4 route flush table 100 2>/dev/null || true
  if wg_restart_and_wait; then echo "tunnel up, hub $HUB_IP answers"; else echo "WARNING: hub $HUB_IP not answering after the restart: wg show; journalctl -u wg-quick@$WG_IF"; fi
  echo "removed: $EGRESS_UNIT, $EGRESS_BIN, $EGRESS_SYSCTL, $WG_DROPIN. Containers now leave via the LAN."
  exit 0
fi

# --- maintenance mode: `touch <dir>/egress` then run (over the LAN alias: the tunnel restarts); routes the
# internet traffic of every container (app, db, crawl: $CONTAINER_NETS) through the hub, exit address
# $HUB_PUBLIC_IP, and refuses it when the tunnel is down. The host keeps its LAN default route. Pieces:
#   wg0.conf   [Interface] Table = off; PostUp = ip route replace default dev wg0 table 100; PostDown = ip route
#              flush table 100   [Peer] AllowedIPs = 0.0.0.0/0 (PersistentKeepalive = 25 stays). Backed up first;
#              edited with awk; the PrivateKey is never printed.
#   policy     $EGRESS_BIN (deploy/systemd/jobboard-egress.sh) + $EGRESS_UNIT: ip rules 98-101 (see the script),
#              also wanted by wg-quick@wg0; $EGRESS_SYSCTL: loose rp_filter.
#   verify     hub answers over wg0 within 20 s of the restart, else the backup is restored and the tunnel
#              restarted again (exit 1); then the exit address seen from a container must be $HUB_PUBLIC_IP.
# Re-runnable: an unchanged wg0.conf means no restart; the rules are reconciled in place.
if [ -f "$HERE/egress" ]; then
  rm -f "$HERE/egress"
  SRC=$APP/deploy/systemd
  [ -f "$SRC/jobboard-egress.sh" ] && [ -f "$SRC/jobboard-egress.service" ] || { echo "no $SRC/jobboard-egress.*: run the full bootstrap first"; exit 1; }
  [ -f "$WG_CONF" ] || { echo "no $WG_CONF"; exit 1; }
  [ "$(grep -c '^\[Peer\]' "$WG_CONF")" = 1 ] || { echo "$WG_CONF does not have exactly one [Peer]; edit it by hand (see the header above)"; exit 1; }
  docker info >/dev/null 2>&1 || { echo "docker daemon not running"; exit 1; }
  log "egress: containers in $CONTAINER_NETS -> $WG_IF -> hub $HUB_IP -> internet as $HUB_PUBLIC_IP; prohibited while the tunnel is down"

  BK=/root/jobboard-bootstrap-backup/$(date +%Y%m%d-%H%M%S); install -d -m 700 "$BK"
  install -m 600 -o root -g root "$WG_CONF" "$BK/$WG_IF.conf"
  [ -f "$WG_ORIG" ] || install -m 600 -o root -g root "$WG_CONF" "$WG_ORIG"
  echo "backup: $BK/$WG_IF.conf (pre-egress original kept at $WG_ORIG for egress-off)"

  NEW=$(mktemp)
  # Only the keys below change; every other line (PrivateKey, Address, MTU, PublicKey, Endpoint, PresharedKey,
  # other Post* lines) passes through. Missing keys are added at the end of their section.
  awk '
  function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
  function key(l,  k) { k = l; sub(/=.*/, "", k); return tolower(trim(k)) }
  function end_iface() { if (!have_table) print "Table = off"; for (i = 1; i <= 2; i++) if (!have_post[i]) print post[i] }
  function end_peer()  { if (!have_allowed) print "AllowedIPs = 0.0.0.0/0"; if (!have_ka) print "PersistentKeepalive = 25" }
  BEGIN { post[1] = "PostUp = ip route replace default dev '"$WG_IF"' table 100"; post[2] = "PostDown = ip route flush table 100"; sec = "" }
  /^[ \t]*$/       { blank = blank $0 "\n"; next }
  /^\[Interface\]/ { if (sec == "peer") end_peer(); printf "%s", blank; blank = ""; sec = "iface"; print; next }
  /^\[Peer\]/      { if (sec == "iface") end_iface(); printf "%s", blank; blank = ""; sec = "peer"; print; next }
  {
    printf "%s", blank; blank = ""
    if (sec == "iface") {
      k = key($0)
      if (k == "table") { if (!have_table) { print "Table = off"; have_table = 1 }; next }
      if (k == "postup" || k == "postdown") { for (i = 1; i <= 2; i++) if (trim($0) == post[i]) have_post[i] = 1; print; next }
    } else if (sec == "peer") {
      k = key($0)
      if (k == "allowedips") { if (!have_allowed) { print "AllowedIPs = 0.0.0.0/0"; have_allowed = 1 }; next }
      if (k == "persistentkeepalive") { if (!have_ka) { print "PersistentKeepalive = 25"; have_ka = 1 }; next }
    }
    print
  }
  END { if (sec == "iface") end_iface(); if (sec == "peer") end_peer(); printf "%s", blank }
  ' "$WG_CONF" > "$NEW"
  if cmp -s "$WG_CONF" "$NEW"; then
    WG_CHANGED=0; echo "$WG_CONF already as wanted"
  else
    WG_CHANGED=1; echo "$WG_CONF changes (PrivateKey redacted):"
    diff -u "$WG_CONF" "$NEW" | sed -E 's/^([-+ ]?PrivateKey *=).*/\1 <redacted>/' | sed 's/^/    /' || true
    install -m 600 -o root -g root "$NEW" "$WG_CONF"
  fi
  rm -f "$NEW"

  sed -e "s|@CONTAINER_NETS@|$CONTAINER_NETS|g" -e "s|@WG_NET@|$WG_NET|g" -e "s|@WG_IF@|$WG_IF|g" "$SRC/jobboard-egress.sh" > "$EGRESS_BIN"
  chmod 755 "$EGRESS_BIN"; chown root:root "$EGRESS_BIN"
  install -m 644 -o root -g root "$SRC/jobboard-egress.service" "/etc/systemd/system/$EGRESS_UNIT"
  cat > "$EGRESS_SYSCTL" <<EOF
# jobboard egress (deploy/bootstrap.sh egress mode): loose reverse-path filtering, because replies for the
# containers' traffic (masqueraded to $WG_IP, routed via table 100) arrive on $WG_IF while the main table
# would route their sources via the LAN. The leading "-" tolerates a missing $WG_IF at boot; systemd-sysctl
# re-applies the key when the interface appears.
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
-net.ipv4.conf.$WG_IF.rp_filter = 2
EOF
  install -d "$(dirname "$WG_DROPIN")" "$(dirname "$DOCKER_DROPIN")"
  cat > "$WG_DROPIN" <<EOF
# jobboard egress (deploy/bootstrap.sh): the policy rules go with the tunnel
[Unit]
Wants=$EGRESS_UNIT
EOF
  # dockerd restores the restart:unless-stopped containers as soon as it starts; the prohibit rule must be
  # there first, or a reboot leaks container traffic via the LAN until the unit runs. The unit itself is
  # Before=docker.service; this drop-in makes docker pull it in even if the unit were enabled late.
  cat > "$DOCKER_DROPIN" <<EOF
# jobboard egress (deploy/bootstrap.sh): containers must not start before the egress rules exist
[Unit]
After=$EGRESS_UNIT
Wants=$EGRESS_UNIT
EOF
  systemctl daemon-reload
  sysctl -q -p "$EGRESS_SYSCTL" 2>/dev/null || sysctl -q -w net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2
  systemctl enable -q "$EGRESS_UNIT"
  # oneshot+RemainAfterExit: `start` is a no-op while active and `restart` would drop the prohibit rule for a
  # moment, so a live host reconciles in place.
  if systemctl is-active -q "$EGRESS_UNIT"; then "$EGRESS_BIN" apply; else systemctl start "$EGRESS_UNIT"; fi

  if [ "$WG_CHANGED" = 1 ] || ! ip -4 route show table 100 2>/dev/null | grep -q "^default dev $WG_IF"; then
    echo "restarting wg-quick@$WG_IF (the LAN route is untouched: Table = off; a session over the tunnel would drop)"
    if ! wg_restart_and_wait; then
      echo "hub $HUB_IP did not answer within 20 s: restoring $BK/$WG_IF.conf and restarting again"
      install -m 600 -o root -g root "$BK/$WG_IF.conf" "$WG_CONF"
      if wg_restart_and_wait; then echo "tunnel back on the previous config"; else echo "tunnel still down on the previous config: wg show; journalctl -u wg-quick@$WG_IF"; fi
      echo "policy rules left in place: without table 100 they only refuse container traffic (kill switch). Fix, then re-run egress; or egress-off."
      exit 1
    fi
    echo "tunnel up, hub $HUB_IP answers"
  fi
  "$EGRESS_BIN" verify
  wg show "$WG_IF" 2>/dev/null | grep -E 'allowed ips|persistent keepalive|latest handshake' | sed 's/^/  /' || true
  echo "host default route (must stay the LAN): $(ip -4 route show default | head -1)"
  # The rules only match $CONTAINER_NETS: a network compose left on an old dynamic subnet would bypass them.
  log "container networks must lie in $CONTAINER_NETS"
  if ! container_nets_ok; then
    echo "recreating the app stack so compose applies the fixed subnet (compose down, then the deploy helper; crawl is external and kept)"
    C down --remove-orphans || true
    /usr/local/sbin/jobboard-compose-up
    container_nets_ok || { echo "ABORT: a network is still outside $CONTAINER_NETS; its containers are not covered. Rules left in place. Fix it (crawl: docker compose -p crawl down; docker network rm crawl; re-run the full bootstrap) and re-run egress."; exit 1; }
  fi
  exit_ip_check || true
  echo "kill-switch test: sudo wg-quick down $WG_IF; docker exec jobboard python3 -c '...' fails at once; sudo wg-quick up $WG_IF restores it"
  echo "undo: touch $HERE/egress-off && sudo bash $HERE/bootstrap.sh"
  exit 0
fi

# ================================================================================================
# Full run from here: host-specific work for the desktop.
# ================================================================================================

# Keys: needed on the first run; on re-runs the installed copies are reused when the staged files are absent.
[ -f "$HERE/github_deploy_key" ] || [ -f /home/deploy/.ssh/github_deploy_key ] || { echo "missing $HERE/github_deploy_key"; exit 1; }
[ -f "$HERE/jobboard_deploy.pub" ] || [ -f /home/deploy/.ssh/deploy_keys.pub ] || { echo "missing $HERE/jobboard_deploy.pub"; exit 1; }
# Certificates: the site is never enabled without them (nginx would fail to start, taking MCP with it).
CERTS=/etc/nginx/jobboard/certs
for f in origin.pem origin.key cloudflare-origin-pull-ca.pem; do
  [ -f "$HERE/$f" ] || [ -f "$CERTS/$f" ] || { echo "missing $HERE/$f (stage the Cloudflare origin cert, key and origin-pull CA); refusing to enable the site"; exit 1; }
done

log "backups"
mkdir -p /root/jobboard-bootstrap-backup
nft list ruleset > "/root/jobboard-bootstrap-backup/nft-ruleset-$(date +%s).nft" 2>/dev/null || true
cp -a /etc/nftables.conf /root/jobboard-bootstrap-backup/ 2>/dev/null || true
cp -a /etc/nginx "/root/jobboard-bootstrap-backup/nginx-$(date +%s)" 2>/dev/null || true
echo "saved under /root/jobboard-bootstrap-backup (restore firewall table: nft -f <file>)"

log "packages (docker is required, not installed: Docker CE is already on this host)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
if ! apt-get install -y -qq git nginx nftables curl python3 >/dev/null 2>&1; then
  echo "apt-get install failed; continuing with the tools already present:"
fi
for t in git nginx nft curl docker wg python3; do command -v "$t" >/dev/null || { echo "missing required tool: $t"; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not running"; exit 1; }
ip -4 addr show "$WG_IF" 2>/dev/null | grep -q "inet $WG_IP/" || { echo "$WG_IF does not carry $WG_IP; bring WireGuard up first (wg-quick@$WG_IF)"; exit 1; }
grep -qE '^\s*PersistentKeepalive\s*=\s*25' "/etc/wireguard/$WG_IF.conf" 2>/dev/null \
  || echo "WARNING: /etc/wireguard/$WG_IF.conf has no PersistentKeepalive = 25; the inbound forward from the hub depends on it"
ss -tln | awk '{print $4}' | grep -qE "[:.]$SSH_PORT$" || echo "WARNING: nothing listens on port $SSH_PORT; the hub forwards ssh there"
echo "git nginx nft curl docker wg python3 present; $WG_IF = $WG_IP"

log "deploy user"
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
# deploy is NOT in the docker group (docker socket = root). Container work goes through the sudo helper.
gpasswd -d deploy docker >/dev/null 2>&1 || true
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
[ -f "$HERE/jobboard_deploy.pub" ] && install -m 600 -o deploy -g deploy "$HERE/jobboard_deploy.pub" /home/deploy/.ssh/deploy_keys.pub
[ -f "$HERE/github_deploy_key" ] && install -m 600 -o deploy -g deploy "$HERE/github_deploy_key" /home/deploy/.ssh/github_deploy_key
# Every deploy key is pinned to the forced command: the key can only run jobboard-deploy, nothing else.
OPTS='command="/usr/local/bin/jobboard-deploy",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty,restrict'
sed -E "s#^#$OPTS #" /home/deploy/.ssh/deploy_keys.pub > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys; chmod 600 /home/deploy/.ssh/authorized_keys
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
echo "sshd for deploy: $(sshd -T -C user=deploy,host=x,addr=203.0.113.1 2>/dev/null | grep -E '^(port|allowusers|pubkeyauthentication|strictmodes|authorizedkeysfile)' | tr '\n' ' ')"
echo "authorized_keys: $(ssh-keygen -lf /home/deploy/.ssh/authorized_keys 2>&1)"
echo "shadow: $(getent shadow deploy | cut -d: -f2 | cut -c1-2)…  home: $(stat -c '%A %U' /home/deploy) .ssh: $(stat -c '%A %U' /home/deploy/.ssh)"

log "checkout $APP at $DEPLOY_REF"
if [ ! -d "$APP/.git" ]; then
  install -d -o deploy -g deploy "$APP"
  su - deploy -c "git clone --quiet $REPO_SSH $APP"
fi
su - deploy -c "cd $APP && git fetch --quiet origin && git reset --hard --quiet $DEPLOY_REF"
git config --system --add safe.directory "$APP" 2>/dev/null || true   # root reads the deploy-owned checkout below
echo "at $(cd "$APP" && git rev-parse --short HEAD): $(cd "$APP" && git log -1 --pretty=%s)"
# Root-owned copies of the deploy scripts: a push cannot change how deploys run.
install -m 755 -o root -g root "$APP/deploy/deploy.sh" /usr/local/bin/jobboard-deploy
install -m 755 -o root -g root "$APP/deploy/compose-up.sh" /usr/local/sbin/jobboard-compose-up
cat > /etc/sudoers.d/jobboard-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/local/sbin/jobboard-compose-up
EOF
chmod 440 /etc/sudoers.d/jobboard-deploy; visudo -c >/dev/null

log "secrets -> $APP/.env"
ADOPTED=0
if [ -f "$HERE/jobboard.env" ]; then
  # A complete .env exported from the old host (export-secrets mode) wins over anything here, so the
  # password hash, cookie secret and keys stay what the user and the Mac already use.
  grep -qE '^POSTGRES_PASSWORD=.+' "$HERE/jobboard.env" || { echo "$HERE/jobboard.env has no POSTGRES_PASSWORD; not adopting"; exit 1; }
  NEWPG=$(sed -nE 's/^POSTGRES_PASSWORD=//p' "$HERE/jobboard.env" | head -1)
  if [ -f "$APP/.env" ] && [ -n "$(C ps -q db 2>/dev/null || true)" ]; then
    # The Postgres volume was initialised with the previous password: rotate it inside the running db
    # (socket connection inside the container is trusted) before the stack restarts with the new .env.
    OLDPG=$(sed -nE 's/^POSTGRES_PASSWORD=//p' "$APP/.env" | head -1)
    if [ "$OLDPG" != "$NEWPG" ]; then
      psql_db jobscan -v ON_ERROR_STOP=1 -c "ALTER USER jobscan PASSWORD '$NEWPG'"
      echo "db password rotated to the adopted value"
    fi
  elif [ -f "$APP/.env" ]; then
    echo "WARNING: $APP/.env exists but the db container is not running: if its POSTGRES_PASSWORD differs from the"
    echo "         staged one, the existing volume keeps the old password and the app will fail to connect (start the stack and re-run)"
  fi
  install -m 600 -o deploy -g deploy "$HERE/jobboard.env" "$APP/.env"
  shred -u "$HERE/jobboard.env" 2>/dev/null || rm -f "$HERE/jobboard.env"
  ADOPTED=1
  echo "adopted $HERE/jobboard.env as $APP/.env (staged copy removed)"
elif [ ! -f "$APP/.env" ]; then
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
# The crawl stack reads the same .env (deploy/crawl/compose.yaml); SearXNG needs its own cookie secret.
grep -q '^SEARXNG_SECRET=' "$APP/.env" || { echo "SEARXNG_SECRET=$(openssl rand -hex 32)" >> "$APP/.env"; echo "SEARXNG_SECRET added"; }

log "nginx (binds $WG_IP only)"
install -d -m 755 /var/www/slindskog.net
install -m 644 "$APP/deploy/homepage.html" /var/www/slindskog.net/index.html
install -d -m 755 /etc/nginx/jobboard
install -d -m 700 "$CERTS"
for f in origin.pem origin.key cloudflare-origin-pull-ca.pem; do
  [ -f "$HERE/$f" ] && install -m 600 -o root -g root "$HERE/$f" "$CERTS/$f" && rm -f "$HERE/$f"
done
openssl x509 -in "$CERTS/origin.pem" -noout -checkend 2592000 >/dev/null || echo "WARNING: origin.pem expires within 30 days (or is unreadable)"
echo "certs: $(openssl x509 -in "$CERTS/origin.pem" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
# The site and the production-only snippets carry @PLACEHOLDERS@ (nginx has no shell variables); the
# dev stack mounts deploy/nginx/ as-is and includes only the placeholder-free auth/web/mcp snippets.
SUBST=(-e "s|@WG_IP@|$WG_IP|g" -e "s|@HUB_IP@|$HUB_IP|g" -e "s|@MCP_PORT@|$MCP_PORT|g" -e "s|@WG_NET@|$WG_NET|g")
for f in "$APP"/deploy/nginx/jobboard-*.conf; do
  sed "${SUBST[@]}" "$f" > "/etc/nginx/jobboard/$(basename "$f")"; chmod 644 "/etc/nginx/jobboard/$(basename "$f")"
done
sed "${SUBST[@]}" "$APP/deploy/nginx/jobboard.conf" > /etc/nginx/sites-available/jobboard.conf
chmod 644 /etc/nginx/sites-available/jobboard.conf
if grep -lE '@[A-Z_]+@' /etc/nginx/sites-available/jobboard.conf /etc/nginx/jobboard/*.conf; then echo "unsubstituted placeholder above"; exit 1; fi
ln -sf /etc/nginx/sites-available/jobboard.conf /etc/nginx/sites-enabled/jobboard.conf
rm -f /etc/nginx/sites-enabled/default
# nginx binds $WG_IP, which exists only once wg0 is up: let it bind regardless (nonlocal bind) and start
# after WireGuard, so a reboot never leaves nginx dead because the tunnel came up a second later.
cat > /etc/sysctl.d/90-jobboard.conf <<'EOF'
# jobboard (deploy/bootstrap.sh): nginx listens on the WireGuard address, which may appear after nginx starts
net.ipv4.ip_nonlocal_bind = 1
EOF
sysctl -q -p /etc/sysctl.d/90-jobboard.conf
install -d /etc/systemd/system/nginx.service.d
cat > /etc/systemd/system/nginx.service.d/jobboard.conf <<EOF
# jobboard (deploy/bootstrap.sh): the listeners live on $WG_IF
[Unit]
After=wg-quick@$WG_IF.service
EOF
systemctl daemon-reload
nginx -t
echo "nginx config installed; reloaded after the deploy step"

log "firewall (nftables: filter packets arriving on $WG_IF only; policy accept elsewhere; Docker's tables untouched)"
install -d /etc/nftables.d
# declare / flush / define: `nft -f` of this file is idempotent and never needs `flush ruleset` (which
# would wipe Docker's iptables-nft tables and wg-quick's rules). This host is a personal machine on a
# home LAN: no input-drop policy anywhere; only the tunnel is filtered, because the hub forwards
# public traffic down it.
cat > /etc/nftables.d/jobboard.nft <<EOF
# jobboard: filter for packets arriving on $WG_IF — installed by deploy/bootstrap.sh
# Check: nft -c -f /etc/nftables.d/jobboard.nft   Show: nft list table inet jobboard
table inet jobboard {}
flush table inet jobboard
table inet jobboard {
  chain input {
    type filter hook input priority filter; policy accept;
    iifname "$WG_IF" jump wg0_in
  }
  chain wg0_in {
    ct state established,related accept
    ct state invalid drop
    ip saddr $HUB_IP tcp dport { 443, $SSH_PORT } accept        comment "hub: DNAT'd + masqueraded public flows (cloudflare 443, ssh 2222)"
    ip saddr $LAPTOP_IP tcp dport { $SSH_PORT, $MCP_PORT } accept  comment "laptop: ssh, MCP"
    ip saddr { $HUB_IP, $LAPTOP_IP } icmp type echo-request accept comment "peer ping (wg watchdog on the hub side too)"
    counter drop                                               comment "anything else from the tunnel"
  }
}
EOF
touch /etc/nftables.conf
grep -q 'nftables.d/jobboard.nft' /etc/nftables.conf || echo 'include "/etc/nftables.d/jobboard.nft"' >> /etc/nftables.conf
grep -q '^#!/usr/sbin/nft -f' /etc/nftables.conf || sed -i '1i #!/usr/sbin/nft -f' /etc/nftables.conf
nft -c -f /etc/nftables.conf                       # the boot config parses (never run live: it may flush ruleset)
nft -f /etc/nftables.d/jobboard.nft                 # live: just our table, atomically replaced
systemctl enable nftables >/dev/null 2>&1 || true   # boot persistence only; never `restart nftables` on a Docker host
echo "wg0_in: $HUB_IP -> {443,$SSH_PORT}; $LAPTOP_IP -> {$SSH_PORT,$MCP_PORT}; ping; rest dropped. Other interfaces: untouched."
OTHER_DROP=$(nft list chains 2>/dev/null | awk '/^table /{t=$2" "$3} /hook input .*policy drop/{print t}' | sort -u | tr '\n' ' ')
[ -z "$OTHER_DROP" ] || echo "NOTE: input chains with policy drop exist in: $OTHER_DROP — they must also admit the flows above"

log "wireguard watchdog"
install -m 755 -o root -g root "$APP/deploy/systemd/jobboard-wg-watch.sh" /usr/local/sbin/jobboard-wg-watch
for u in jobboard-wg-watch.service jobboard-wg-watch.timer; do
  sed -e "s|@HUB_IP@|$HUB_IP|g" -e "s|@WG_IF@|$WG_IF|g" "$APP/deploy/systemd/$u" > "/etc/systemd/system/$u"; chmod 644 "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now jobboard-wg-watch.timer >/dev/null
echo "every 2 min: ping $HUB_IP over $WG_IF; 3 failing runs -> systemctl restart wg-quick@$WG_IF (journalctl -u jobboard-wg-watch)"

log "deploy $DEPLOY_REF$([ -f "$HERE/deploy-ref" ] && echo " (from $HERE/deploy-ref; delete it once that branch has merged)")"
ensure_crawl_network                                 # compose.prod.yaml joins it (external)
su - deploy -c "/usr/local/bin/jobboard-deploy $DEPLOY_REF"
systemctl reload nginx
echo "nginx reloaded: jobs.slindskog.net on $WG_IP:443 -> 127.0.0.1:8080 (login paths open, everything else needs a cookie or token),"
echo "                mcp $WG_IP:$MCP_PORT -> 127.0.0.1:8091 (Bearer only), homepage, default 444"

if [ "$CRAWL" = 1 ]; then
  log "crawl stack (Firecrawl + SearXNG, project crawl, network crawl, no published ports)"
  # SearXNG settings: settings.yml cannot read env, so the template is rendered here with the app's proxy
  # list (JOBSCAN_PROXY, same parser as the app) and the result mounted instead (compose var SEARXNG_SETTINGS,
  # persisted in .env so a hand-run `docker compose -p crawl` uses it too). It holds the proxy password:
  # root:977 0640: the entrypoint runs as root with every capability dropped (no DAC override, so it
  # can only read files it owns), the worker as the image's searxng user, uid/gid 977.
  SEARXNG_SETTINGS=/etc/jobboard/searxng-settings.yml
  install -d -m 755 /etc/jobboard
  RENDERED=$(mktemp)
  python3 "$APP/deploy/crawl/searxng/render-settings.py" "$APP/deploy/crawl/searxng/settings.yml" "$APP/.env" "$APP/jobscan" > "$RENDERED"
  SEARXNG_RECREATE=()
  if [ -f "$SEARXNG_SETTINGS" ] && cmp -s "$SEARXNG_SETTINGS" "$RENDERED"; then
    echo "searxng settings unchanged ($SEARXNG_SETTINGS)"
  else
    # A bind-mounted file follows the inode: the running container keeps the old content until recreated,
    # and `up -d` does not notice content changes, so force it.
    SEARXNG_RECREATE=(--force-recreate); echo "searxng settings rendered -> $SEARXNG_SETTINGS (searxng will be recreated)"
  fi
  install -m 640 -o root -g 977 "$RENDERED" "$SEARXNG_SETTINGS"   # always: owner and mode are part of the contract
  rm -f "$RENDERED"
  if grep -q '^SEARXNG_SETTINGS=' "$APP/.env"; then sed -i "s|^SEARXNG_SETTINGS=.*|SEARXNG_SETTINGS=$SEARXNG_SETTINGS|" "$APP/.env"
  else echo "SEARXNG_SETTINGS=$SEARXNG_SETTINGS" >> "$APP/.env"; fi
  # Ships through the bootstrap, not through a push: a deploy only rebuilds the app.
  (cd "$APP" && docker compose -f deploy/crawl/compose.yaml --env-file .env -p crawl up -d --remove-orphans)
  [ ${#SEARXNG_RECREATE[@]} -eq 0 ] || (cd "$APP" && docker compose -f deploy/crawl/compose.yaml --env-file .env -p crawl up -d "${SEARXNG_RECREATE[@]}" searxng)
  docker compose -p crawl ps --format 'table {{.Name}}\t{{.Status}}'
fi

log "egress via the hub (reported only; the egress / egress-off modes change it)"
if systemctl is-active -q "$EGRESS_UNIT" 2>/dev/null; then
  echo "active: containers in $CONTAINER_NETS leave via $HUB_IP as $HUB_PUBLIC_IP, prohibited while the tunnel is down"
  [ -x "$EGRESS_BIN" ] && { "$EGRESS_BIN" verify || true; }
  container_nets_ok || echo "WARNING: a network above lies outside $CONTAINER_NETS, so its containers bypass the egress rules; the egress mode recreates the stack to fix the app network"
else
  echo "not enabled: containers use the host's LAN route. touch $HERE/egress && sudo bash $HERE/bootstrap.sh to route them via the hub"
  container_nets_ok || echo "NOTE: a network above lies outside $CONTAINER_NETS; the egress mode will recreate the stack"
fi

log "database restore (if a dump is staged and the jobs table is empty)"
DUMP="$HERE/jobscan-dump.sql.gz"
HAD_DUMP=0
if [ -f "$DUMP" ]; then
  HAD_DUMP=1
  N=$(psql_db jobscan -c 'SELECT COUNT(*) FROM jobs' 2>/dev/null | tail -1 || echo "?")
  if [ "$N" = "0" ]; then
    gunzip -c "$DUMP" | C exec -T db psql -U jobscan -d jobscan -q -v ON_ERROR_STOP=0 >/tmp/jobscan-restore.log 2>&1 || true
    N2=$(psql_db jobscan -c 'SELECT COUNT(*) FROM jobs' 2>/dev/null | tail -1)
    echo "restored: jobs=$N2 (log: /tmp/jobscan-restore.log, errors: $(grep -c ERROR /tmp/jobscan-restore.log || true))"
    rm -f "$DUMP"
  else
    echo "jobs table has $N rows; dump left untouched (use the restore mode to replace the database)"
  fi
else
  echo "no dump staged at $DUMP"
fi

log "api token for Claude"
HAVE=$(psql_db jobscan -c "SELECT COUNT(*) FROM auth_tokens WHERE name='claude-mac' AND revoked_at IS NULL" 2>/dev/null | tail -1 || echo 0)
if [ "$ADOPTED" = 1 ] || [ "$HAD_DUMP" = 1 ]; then
  echo "staged env/dump present: the Mac's existing token applies once the database is restored; not minting (active claude-mac tokens: ${HAVE:-0})"
elif [ "${HAVE:-0}" != "0" ]; then
  echo "an active claude-mac token already exists; not minting another (revoke it via the API to re-mint)"
elif [ ! -f "$HERE/claude-token" ]; then
  TOK=$(C exec -T jobboard python3 server.py issue-token claude-mac 2>/dev/null | tail -1)
  if [ -n "$TOK" ]; then
    install -m 600 -o "$OWNER" /dev/null "$HERE/claude-token"
    echo "$TOK" > "$HERE/claude-token"
    echo "token written to $HERE/claude-token (delete after copying)"
  else
    echo "token issuance failed; later: docker compose ... exec jobboard python3 server.py issue-token <name>"
  fi
else
  echo "exists, kept"
fi

log "done"
echo "Web: https://jobs.slindskog.net reaches this host only via the hub's 443 DNAT (hub-bootstrap.sh enable-dnat)."
echo "MCP: http://$WG_IP:$MCP_PORT/mcp over WireGuard, Authorization: Bearer <token>."
echo "Backup: touch $HERE/dump && sudo bash $HERE/bootstrap.sh  ->  $HERE/jobscan-dump.sql.gz"
echo "Egress: $(systemctl is-active "$EGRESS_UNIT" 2>/dev/null || echo inactive) (touch $HERE/egress to route container traffic via the hub; hub-bootstrap.sh apply first)"
