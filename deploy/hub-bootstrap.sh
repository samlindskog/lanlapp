#!/usr/bin/env bash
# Hub bootstrap for uscentral1: turns the VPS into a pure WireGuard hub + forwarder for the jobboard that now
# runs on the desktop (10.88.0.2). Run as root, re-runnable:
#   sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh [--check] [apply|enable-dnat|disable-dnat|retire-app]
#   --check       dry run: print the analysis, the planned files (as diffs) and the live transaction; change nothing
#   apply         (default) install/refresh everything below; the 443 DNAT is included only if its flag exists
#   enable-dnat   create /etc/jobboard-hub/dnat-443, regenerate and reload: public 443 now reaches the desktop
#   disable-dnat  remove the flag, regenerate and reload: 443 stays on the hub (rollback)
#   retire-app    needs `touch <this dir>/retire-app`: remove the OLD jobboard installation from the hub
#
# Installs:
#   /etc/nftables.d/jobboard-hub.nft        table inet jobboard_hub (declare/flush/define: `nft -f` is idempotent)
#     sets cloudflare_v4/v6                 elements from /etc/nftables.d/jobboard-cloudflare-elements.nft
#     input  (policy accept)                443 accepted from Cloudflare only, any other 443 dropped
#     prerouting nat                        [flag] eth0 443 from Cloudflare -> 10.88.0.2:443; 2222 -> 10.88.0.2:1025
#                                           only if no other table already has that DNAT (it predates this script)
#     forward (policy accept)               MSS clamp; established; eth0->wg0 to the desktop {443,1025};
#                                           laptop->desktop {1025,8090} wg0->wg0 (routed, not NATed); peer ping;
#                                           desktop -> eth0 (its containers' egress, policy-routed there)
#     postrouting nat                       masquerade the DNAT'd public flows towards the desktop (source = 10.88.0.1);
#                                           masquerade 10.88.0.2 out of eth0 (the containers exit as this host)
#   /etc/nftables.conf                      marked block: include line + accept rules inserted into OTHER tables'
#                                           drop-policy forward chains (see "evaluation semantics" below)
#   /usr/local/sbin/jobboard-hub-accepts    + .service: the same accepts for drop chains NOT defined in
#                                           /etc/nftables.conf (Docker's iptables-nft FORWARD), re-inserted after
#                                           docker.service at boot, because a boot-time `insert rule` into a chain
#                                           that does not exist yet would fail the whole nftables.service load
#   /usr/local/sbin/jobboard-cloudflare-ips + .service/.timer (weekly): fetch ips-v4/ips-v6, nft -c, reload.
#                                           apply and --check fetch the ranges FIRST (into a temp file; apply
#                                           installs it before any other change and aborts if the fetch fails),
#                                           so the table never goes live with empty sets while 443 is production
#   /etc/sysctl.d/90-jobboard-hub.conf      net.ipv4.ip_forward=1
#
# EVALUATION SEMANTICS: every base chain on a hook sees every packet; `accept` ends only its own chain. A packet
# is dropped if ANY chain on the hook drops it, so an accept in jobboard_hub cannot override `policy drop` in a
# pre-existing table (unknown until `show-firewall` runs). This script therefore detects every forward-hook base
# chain with policy drop and inserts equivalent accept rules tagged `comment "jobboard-hub"` at its top. Our own
# `drop` rules are authoritative regardless. Nothing here ever runs `flush ruleset` or restarts nftables.service
# on the live host (both would wipe Docker's and wg-quick's dynamic rules).
set -euo pipefail

# --- parameters -------------------------------------------------------------------------------------
WAN_IF=eth0                                # public interface (verified against the default route)
WG_IF=wg0
HUB_IP=10.88.0.1
DESKTOP_IP=10.88.0.2
LAPTOP_IP=10.88.0.3
HTTPS_PORT=443
SSH_FWD_PORT=2222                          # public ssh port forwarded to the desktop (pre-existing DNAT)
DESKTOP_SSH_PORT=1025
MCP_PORT=8090
NFT_MAIN=/etc/nftables.conf
NFT_DIR=/etc/nftables.d
HUB_NFT=$NFT_DIR/jobboard-hub.nft
ELEM=$NFT_DIR/jobboard-cloudflare-elements.nft
CONF_DIR=/etc/jobboard-hub
DNAT_FLAG=$CONF_DIR/dnat-443
ACCEPTS_BIN=/usr/local/sbin/jobboard-hub-accepts
CFIPS_BIN=/usr/local/sbin/jobboard-cloudflare-ips
TAG='comment "jobboard-hub"'
BEGIN_MARK='# BEGIN jobboard-hub (managed by deploy/hub-bootstrap.sh; do not edit by hand)'
END_MARK='# END jobboard-hub'
OLD_APP=/opt/jobboard
HERE="$(cd "$(dirname "$0")" && pwd)"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP=/root/jobboard-hub-backup/$TS

CHECK=0; MODE=apply
for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    apply|enable-dnat|disable-dnat|retire-app) MODE=$a ;;
    *) sed -n '2,8p' "$0"; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
for t in nft curl ip systemctl awk; do command -v "$t" >/dev/null || { echo "missing required tool: $t"; exit 1; }; done
log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
plan() { printf '  + %s\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
# Every change goes through these two, so --check prints exactly what apply would do.
run() { if [ "$CHECK" = 1 ]; then plan "$*"; else "$@"; fi; }
# install_content <dest> <mode>  (content on stdin): diff + install, or diff only with --check
install_content() {
  local dest=$1 mode=$2 tmp; tmp=$(mktemp); cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$dest" "$tmp"; then echo "  unchanged: $dest"
  elif [ -f "$dest" ]; then
    echo "  write $dest ($mode), diff:"; diff -u "$dest" "$tmp" | sed 's/^/      /' | head -80 || true
    [ "$CHECK" = 1 ] || install -m "$mode" -o root -g root "$tmp" "$dest"
  else
    echo "  create $dest ($mode):"; sed 's/^/      /' "$tmp" | head -80
    [ "$CHECK" = 1 ] || install -m "$mode" -o root -g root "$tmp" "$dest"
  fi
  rm -f "$tmp"
}
backup() {
  [ "$CHECK" = 1 ] && { plan "backup $NFT_MAIN $NFT_DIR nft-ruleset -> $BACKUP"; return; }
  mkdir -p "$BACKUP"
  cp -a "$NFT_MAIN" "$BACKUP/" 2>/dev/null || true
  [ -d "$NFT_DIR" ] && cp -a "$NFT_DIR" "$BACKUP/" || true
  nft list ruleset > "$BACKUP/nft-ruleset.nft" 2>/dev/null || true
  echo "  saved under $BACKUP (boot config: cp back; live rules: nft -f nft-ruleset.nft only on a host without Docker)"
}

# ================================================================================================
# retire-app: remove the old jobboard installation from the hub (after cutover has settled)
# ================================================================================================
if [ "$MODE" = retire-app ]; then
  log "retire-app: remove the OLD jobboard installation from $(hostname)"
  [ -f "$DNAT_FLAG" ] || warn "$DNAT_FLAG is absent: public 443 is NOT forwarded to the desktop yet. Is the cutover done?"
  echo "  plan:"
  plan "stop the stack in $OLD_APP (compose down; the pgdata volume is KEPT as a cold copy)"
  plan "nft delete table inet jobboard; remove its include from $NFT_MAIN; rm $NFT_DIR/jobboard.nft"
  plan "rm nginx site jobboard.conf (sites-enabled + sites-available), /etc/nginx/jobboard, /var/www/slindskog.net; reload or stop nginx"
  plan "rm -rf $OLD_APP; userdel -r deploy; rm /etc/sudoers.d/jobboard-deploy /usr/local/bin/jobboard-deploy /usr/local/sbin/jobboard-compose-up"
  echo "  the old table inet jobboard holds this host's input policy (drop; ssh, wireguard, 443 accepted). After removal the"
  echo "  input hook is whatever the remaining tables do:"
  nft list chains 2>/dev/null | awk '/^table /{t=$2" "$3} /hook input/ && t!="inet jobboard" {print "      " t ": " $0}' | sed 's/[[:space:]]\+/ /g' || true
  if [ "$CHECK" = 1 ] || [ ! -f "$HERE/retire-app" ]; then
    echo; echo "dry run. To execute: touch $HERE/retire-app && sudo bash $0 retire-app"; exit 0
  fi
  rm -f "$HERE/retire-app"
  backup
  if [ -f "$OLD_APP/.env" ] && [ -f "$OLD_APP/jobboard/compose.prod.yaml" ] && command -v docker >/dev/null; then
    (cd "$OLD_APP" && docker compose -f jobboard/compose.prod.yaml --env-file .env down --remove-orphans) || warn "compose down failed; continuing"
    echo "  stack stopped; volume kept: $(docker volume ls -q | grep -E '^jobboard_pgdata$' || echo '(none)')  (remove later: docker volume rm jobboard_pgdata)"
  fi
  nft list table inet jobboard >/dev/null 2>&1 && nft delete table inet jobboard && echo "  deleted live table inet jobboard"
  sed -i '\#include "/etc/nftables.d/jobboard.nft"#d' "$NFT_MAIN"
  rm -f "$NFT_DIR/jobboard.nft"
  nft -c -f "$NFT_MAIN" && echo "  $NFT_MAIN parses without the old include"
  rm -f /etc/nginx/sites-enabled/jobboard.conf /etc/nginx/sites-available/jobboard.conf
  rm -rf /etc/nginx/jobboard /var/www/slindskog.net
  if [ -n "$(ls -A /etc/nginx/sites-enabled 2>/dev/null)" ]; then
    nginx -t && systemctl reload nginx && echo "  nginx reloaded; other sites remain: $(ls /etc/nginx/sites-enabled | tr '\n' ' ')"
  else
    systemctl disable --now nginx >/dev/null 2>&1 && echo "  nginx stopped and disabled (no sites left)"
  fi
  pkill -u deploy 2>/dev/null || true
  id deploy >/dev/null 2>&1 && userdel -r deploy 2>/dev/null && echo "  deploy user removed"
  rm -rf "$OLD_APP" /etc/sudoers.d/jobboard-deploy /usr/local/bin/jobboard-deploy /usr/local/sbin/jobboard-compose-up
  visudo -c >/dev/null
  echo "  done. Left alone: /etc/sudoers.d/jobboard-bootstrap (the old bootstrap's remove-sudo-rule mode), docker itself,"
  echo "  the jobboard_pgdata volume, $BACKUP. Input filtering now:"
  nft list chains | awk '/^table /{t=$2" "$3} /hook input/ {print "      " t ": " $0}' | sed 's/[[:space:]]\+/ /g'
  exit 0
fi

# ================================================================================================
# apply / enable-dnat / disable-dnat
# ================================================================================================
CHECK_NOTE=""; [ "$CHECK" = 1 ] && CHECK_NOTE=" (--check: nothing is changed)"
log "mode $MODE$CHECK_NOTE"
case "$MODE" in
  enable-dnat)  DNAT_ON=1; run mkdir -p "$CONF_DIR"; run touch "$DNAT_FLAG" ;;
  disable-dnat) DNAT_ON=0; run rm -f "$DNAT_FLAG" ;;
  *) if [ -f "$DNAT_FLAG" ]; then DNAT_ON=1; else DNAT_ON=0; fi ;;
esac
echo "  443 DNAT to the desktop: $([ "$DNAT_ON" = 1 ] && echo ENABLED || echo "disabled (hub keeps 443; enable with: $0 enable-dnat)")$CHECK_NOTE"

log "analysis of the live host"
DEF_IF=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
if [ -n "$DEF_IF" ] && [ "$DEF_IF" != "$WAN_IF" ]; then warn "default route is via $DEF_IF, not $WAN_IF: using $DEF_IF"; WAN_IF=$DEF_IF; fi
ip link show "$WG_IF" >/dev/null 2>&1 || warn "$WG_IF does not exist right now (wg-quick@$WG_IF down?); rules still install"
echo "  wan=$WAN_IF wg=$WG_IF desktop=$DESKTOP_IP laptop=$LAPTOP_IP"
LIVE=$(nft list ruleset 2>/dev/null || true)
[ -f "$NFT_MAIN" ] || { warn "$NFT_MAIN does not exist; it will be created"; }
grep -qE '^[[:space:]]*flush ruleset' "$NFT_MAIN" 2>/dev/null && \
  warn "$NFT_MAIN contains 'flush ruleset': never 'systemctl restart nftables' on this host (wipes Docker's and wg-quick's rules); this script applies deltas only"
grep -qE "^[[:space:]]*include[[:space:]]+\"$NFT_DIR/\*" "$NFT_MAIN" 2>/dev/null && \
  warn "$NFT_MAIN includes $NFT_DIR by glob: $ELEM would load on its own (before its table); the assembled-config check below decides"

# Pre-existing ssh forward: keep it, never duplicate it. Only when no table has it does ours carry it.
SSH_DNAT_WHERE=$(printf '%s\n' "$LIVE" | awk -v p="dport $SSH_FWD_PORT" -v d="$DESKTOP_IP:$DESKTOP_SSH_PORT" '
  /^table /{t=$2" "$3} /^\tchain /{c=$2} index($0,p) && /dnat/ && index($0,d) && t!="inet jobboard_hub" {print t" "c; exit}')
if [ -n "$SSH_DNAT_WHERE" ]; then SSH_DNAT_OURS=0; echo "  ssh forward $SSH_FWD_PORT -> $DESKTOP_IP:$DESKTOP_SSH_PORT exists in: $SSH_DNAT_WHERE (kept, not duplicated)"
else SSH_DNAT_OURS=1; warn "no DNAT $SSH_FWD_PORT -> $DESKTOP_IP:$DESKTOP_SSH_PORT found in another table: jobboard_hub will carry it"; fi
SSH_MASQ=$(printf '%s\n' "$LIVE" | awk '/^table /{t=$2" "$3} /masquerade/ && t!="inet jobboard_hub" {print t": "$0}' | sed 's/[[:space:]]\+/ /g')
[ -z "$SSH_MASQ" ] || echo "  existing masquerade rules (other tables):"$'\n'"$(printf '%s\n' "$SSH_MASQ" | sed 's/^/      /')"

# Base chains: "family table chain hook policy"
mapfile -t CHAINS < <(nft list chains 2>/dev/null | awk '
  /^table / { fam=$2; tbl=$3 }
  /^\tchain / { ch=$2 }
  /type (filter|nat|route) hook (input|forward|prerouting|postrouting) / {
    hook=""; pol="accept"
    for (i=1;i<=NF;i++) { if ($i=="hook") hook=$(i+1); if ($i=="policy") { pol=$(i+1); sub(/;$/,"",pol) } }
    print fam, tbl, ch, hook, pol }')
echo "  live base chains (input/forward/nat):"
printf '      %s\n' "${CHAINS[@]:-(none)}"

FWD_RULES=(
  'ct state established,related accept'
  "iifname \"$WAN_IF\" oifname \"$WG_IF\" ip daddr $DESKTOP_IP tcp dport { $HTTPS_PORT, $DESKTOP_SSH_PORT } accept"
  "iifname \"$WG_IF\" oifname \"$WG_IF\" ip saddr $LAPTOP_IP ip daddr $DESKTOP_IP tcp dport { $DESKTOP_SSH_PORT, $MCP_PORT } accept"
  "iifname \"$WG_IF\" oifname \"$WG_IF\" ip saddr { $DESKTOP_IP, $LAPTOP_IP } icmp type echo-request accept"
  "iifname \"$WG_IF\" oifname \"$WAN_IF\" ip saddr $DESKTOP_IP accept"
)
INS_CONF=()   # "family table chain|rule": chains defined in $NFT_MAIN -> persisted in its marked block
INS_LIVE=()   # chains that exist live but not in $NFT_MAIN (Docker) -> re-inserted by the accepts service at boot
for entry in "${CHAINS[@]:-}"; do
  [ -n "$entry" ] || continue
  read -r fam tbl ch hook pol <<< "$entry"
  [ "$hook" = forward ] && [ "$pol" = drop ] || continue
  [ "$tbl" = jobboard_hub ] && continue
  if [ "$fam" != inet ] && [ "$fam" != ip ]; then warn "$fam $tbl $ch has policy drop on forward; family $fam not handled (our flows are IPv4)"; continue; fi
  if grep -qE "^[[:space:]]*table[[:space:]]+${fam}[[:space:]]+${tbl}[[:space:]]*\{" "$NFT_MAIN" 2>/dev/null; then
    for r in "${FWD_RULES[@]}"; do INS_CONF+=("$fam $tbl $ch|$r"); done
    echo "  $fam $tbl $ch: forward policy drop, defined in $NFT_MAIN -> accepts persisted in its marked block"
  else
    for r in "${FWD_RULES[@]}"; do INS_LIVE+=("$fam $tbl $ch|$r"); done
    echo "  $fam $tbl $ch: forward policy drop, NOT in $NFT_MAIN (Docker/iptables-nft?) -> accepts via $ACCEPTS_BIN at boot"
  fi
done
[ ${#INS_CONF[@]} -eq 0 ] && [ ${#INS_LIVE[@]} -eq 0 ] && echo "  no drop-policy forward chains outside jobboard_hub: its own rules suffice"
for ins in "${INS_CONF[@]}" "${INS_LIVE[@]}"; do [ -n "$ins" ] && printf '      insert rule %s %s %s\n' "${ins%%|*}" "${ins#*|}" "$TAG"; done

log "generated files"
gen_hub_nft() {   # $1 = include path for the elements file
  cat <<EOF
#!/usr/sbin/nft -f
# /etc/nftables.d/jobboard-hub.nft — GENERATED by deploy/hub-bootstrap.sh ($MODE, $TS); edit the script, not this.
# WireGuard hub + forwarder for the jobboard on the desktop ($DESKTOP_IP). Self-contained table: declare /
# flush / define, so \`nft -f\` of this file is idempotent and no \`flush ruleset\` is ever needed.
# Check: nft -c -f $HUB_NFT      Show: nft list table inet jobboard_hub
table inet jobboard_hub {}
flush table inet jobboard_hub
table inet jobboard_hub {
	set cloudflare_v4 {
		type ipv4_addr
		flags interval
		comment "https://www.cloudflare.com/ips-v4, refreshed weekly by jobboard-cloudflare-ips.timer"
	}
	set cloudflare_v6 {
		type ipv6_addr
		flags interval
		comment "https://www.cloudflare.com/ips-v6, refreshed weekly by jobboard-cloudflare-ips.timer"
	}

	chain input {
		type filter hook input priority filter; policy accept;
		tcp dport $HTTPS_PORT ip saddr @cloudflare_v4 accept
		tcp dport $HTTPS_PORT ip6 saddr @cloudflare_v6 accept
		tcp dport $HTTPS_PORT counter drop comment "443 only via Cloudflare"
	}

	chain prerouting {
		type nat hook prerouting priority dstnat; policy accept;
$([ "$DNAT_ON" = 1 ] && printf '\t\tiifname "%s" tcp dport %s ip saddr @cloudflare_v4 dnat ip to %s:%s comment "jobs.slindskog.net -> desktop (flag %s)"\n' "$WAN_IF" "$HTTPS_PORT" "$DESKTOP_IP" "$HTTPS_PORT" "$DNAT_FLAG" || printf '\t\t# 443 DNAT disabled: no %s (hub-bootstrap.sh enable-dnat)\n' "$DNAT_FLAG")
$([ "$SSH_DNAT_OURS" = 1 ] && printf '\t\tiifname "%s" tcp dport %s dnat ip to %s:%s comment "public ssh -> desktop sshd"\n' "$WAN_IF" "$SSH_FWD_PORT" "$DESKTOP_IP" "$DESKTOP_SSH_PORT" || printf '\t\t# ssh %s -> %s:%s DNAT exists in another table (%s); not duplicated\n' "$SSH_FWD_PORT" "$DESKTOP_IP" "$DESKTOP_SSH_PORT" "$SSH_DNAT_WHERE")
	}

	chain forward {
		type filter hook forward priority filter; policy accept;
		tcp flags syn tcp option maxseg size set rt mtu comment "MSS clamp: $WG_IF mtu 1420"
		ct state established,related accept
		iifname "$WAN_IF" oifname "$WG_IF" ip daddr $DESKTOP_IP tcp dport { $HTTPS_PORT, $DESKTOP_SSH_PORT } accept comment "DNAT'd public flows -> desktop"
		iifname "$WG_IF" oifname "$WG_IF" ip saddr $LAPTOP_IP ip daddr $DESKTOP_IP tcp dport { $DESKTOP_SSH_PORT, $MCP_PORT } accept comment "laptop -> desktop: ssh, MCP (routed, no NAT)"
		iifname "$WG_IF" oifname "$WG_IF" ip saddr { $DESKTOP_IP, $LAPTOP_IP } icmp type echo-request accept comment "peer ping"
		iifname "$WG_IF" oifname "$WAN_IF" ip saddr $DESKTOP_IP accept comment "desktop egress"
	}

	chain postrouting {
		type nat hook postrouting priority srcnat; policy accept;
		oifname "$WG_IF" ip daddr $DESKTOP_IP ip saddr != ${HUB_IP%.*}.0/24 masquerade comment "DNAT'd public flows: desktop sees $HUB_IP; peer traffic untouched"
		ip saddr $DESKTOP_IP oifname "$WAN_IF" masquerade comment "desktop egress"
	}
}
include "$1"
EOF
}
gen_accepts() {   # the boot-time re-insert for drop chains outside $NFT_MAIN
  cat <<EOF
#!/usr/bin/env bash
# GENERATED by deploy/hub-bootstrap.sh ($TS). Re-inserts the jobboard-hub accept rules into drop-policy forward
# chains that are NOT defined in $NFT_MAIN (Docker's iptables-nft FORWARD), after docker.service has created
# them. Idempotent: in each managed chain the tagged rules are deleted first, then re-inserted; chains that
# do not exist (Docker gone) are skipped. Run by jobboard-hub-accepts.service at boot; safe to run by hand.
set -u
TAG='$TAG'
tx=\$(mktemp); trap 'rm -f "\$tx"' EXIT
# managed_chain <family> <table> <chain>: queue the deletes for that chain's tagged rules
managed_chain() {
  nft -a list chain "\$1" "\$2" "\$3" 2>/dev/null | awk -v tag="\$TAG" -v w="\$1 \$2 \$3" '
    index(\$0, tag) && match(\$0, /# handle [0-9]+\$/) { print "delete rule " w " handle " substr(\$0, RSTART+9) }' >> "\$tx"
}
EOF
  local seen=" " ins where
  for (( i=${#INS_LIVE[@]}-1; i>=0; i-- )); do
    ins=${INS_LIVE[$i]}; where=${ins%%|*}
    if [[ $seen != *" $where "* ]]; then
      seen+="$where "
      printf 'if nft list chain %s >/dev/null 2>&1; then managed_chain %s\n' "$where" "$where"
      for (( j=${#INS_LIVE[@]}-1; j>=0; j-- )); do
        [ "${INS_LIVE[$j]%%|*}" = "$where" ] || continue
        printf "  echo 'insert rule %s %s %s' >> \"\$tx\"\n" "$where" "${INS_LIVE[$j]#*|}" "$TAG"
      done
      printf 'else echo "chain %s absent; skipped"; fi\n' "$where"
    fi
  done
  cat <<'EOF'
if [ -s "$tx" ]; then nft -f "$tx" && echo "jobboard-hub accepts in place: $(grep -c '^insert' "$tx") rule(s)"; else echo "nothing to do"; fi
EOF
}
gen_cfips() {
  cat <<EOF
#!/usr/bin/env bash
# GENERATED by deploy/hub-bootstrap.sh ($TS). Refresh the Cloudflare ranges in table inet jobboard_hub:
# fetch ips-v4 and ips-v6, validate, \`nft -c\`, apply live, then persist to $ELEM (included by $HUB_NFT at boot).
# On any failure the previous elements file (and the live sets) stay as they are. Weekly via the timer.
#   --fetch-only <out>   fetch + validate and write the elements file to <out> only (no nft, no install):
#                        the bootstrap uses this BEFORE it touches the host, so the sets are never empty live.
set -euo pipefail
ELEM=$ELEM
tmp=\$(mktemp); trap 'rm -f "\$tmp"' EXIT
v4=\$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4 | tr -d '\r' | grep -v '^\$' || true)
v6=\$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6 | tr -d '\r' | grep -v '^\$' || true)
n4=\$(printf '%s\n' "\$v4" | grep -cE '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}\$' || true)
n6=\$(printf '%s\n' "\$v6" | grep -cE '^[0-9a-fA-F:]+/[0-9]{1,3}\$' || true)
if [ "\$n4" -lt 5 ] || [ "\$n6" -lt 3 ] || [ "\$n4" -ne "\$(printf '%s\n' "\$v4" | wc -l)" ] || [ "\$n6" -ne "\$(printf '%s\n' "\$v6" | wc -l)" ]; then
  echo "cloudflare ip fetch failed or looked wrong (v4=\$n4 v6=\$n6 valid lines); keeping \$ELEM"; exit 1
fi
{
  echo "# Cloudflare ranges, GENERATED by $CFIPS_BIN on \$(date -u +%FT%TZ); included from $HUB_NFT"
  echo "flush set inet jobboard_hub cloudflare_v4"
  echo "add element inet jobboard_hub cloudflare_v4 { \$(printf '%s\n' "\$v4" | tr '\n' ',' | sed 's/,\$//; s/,/, /g') }"
  echo "flush set inet jobboard_hub cloudflare_v6"
  echo "add element inet jobboard_hub cloudflare_v6 { \$(printf '%s\n' "\$v6" | tr '\n' ',' | sed 's/,\$//; s/,/, /g') }"
} > "\$tmp"
if [ "\${1:-}" = --fetch-only ]; then
  install -m 644 "\$tmp" "\${2:?--fetch-only needs an output path}"
  echo "cloudflare ranges fetched: \$n4 v4 + \$n6 v6 -> \$2"; exit 0
fi
nft -c -f "\$tmp" || { echo "nft -c rejected the new elements; keeping \$ELEM"; exit 1; }
nft -f "\$tmp"
install -m 644 -o root -g root "\$tmp" "\$ELEM"
echo "cloudflare sets refreshed: \$n4 v4 + \$n6 v6 ranges -> \$ELEM"
EOF
}

# --- Cloudflare ranges FIRST ------------------------------------------------------------------------
# The table drops every 443 not in the sets, and this host still serves jobs.slindskog.net until the
# cutover: loading the table with empty sets, even for the seconds before a fetch, would close production
# 443. So the ranges are fetched and validated before anything else; a failed fetch aborts the whole run
# with nothing changed (an elements file already on disk is NOT a fallback: it could be stale).
scratch=$(mktemp -d); trap 'rm -rf "$scratch" "${tx:-}"' EXIT
mkdir -p "$scratch$NFT_DIR"
[ -d "$NFT_DIR" ] && cp -a "$NFT_DIR/." "$scratch$NFT_DIR/"     # whatever $NFT_MAIN already includes from there
log "cloudflare ranges (fetched first; the run aborts here if this fails)"
gen_cfips > "$scratch/cfips"; chmod 755 "$scratch/cfips"
"$scratch/cfips" --fetch-only "$scratch$ELEM" || { echo "ABORT: Cloudflare range fetch failed or validated badly; nothing changed"; exit 1; }
grep -qE '^add element inet jobboard_hub cloudflare_v4 \{ [0-9]' "$scratch$ELEM" || { echo "ABORT: fetched elements file has no v4 ranges; nothing changed"; exit 1; }
echo "  $(grep -c '^add element' "$scratch$ELEM") element statements -> $ELEM ($([ "$CHECK" = 1 ] && echo 'would be' || echo 'is') the first file written)"

# --- assemble the future boot config and check it in a scratch tree -------------------------------
gen_hub_nft "$scratch$ELEM" > "$scratch$HUB_NFT"
block="$BEGIN_MARK"$'\n'"include \"$HUB_NFT\""
for (( i=${#INS_CONF[@]}-1; i>=0; i-- )); do block+=$'\n'"insert rule ${INS_CONF[$i]%%|*} ${INS_CONF[$i]#*|} $TAG"; done
block+=$'\n'"$END_MARK"
newmain="$scratch/nftables.conf.new"
{ [ -f "$NFT_MAIN" ] && awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0==b {skip=1} !skip {print} $0==e {skip=0}' "$NFT_MAIN" || echo '#!/usr/sbin/nft -f'; } > "$newmain"
grep -q '^#!/usr/sbin/nft -f' "$newmain" || sed -i '1i #!/usr/sbin/nft -f' "$newmain"
printf '\n%s\n' "$block" >> "$newmain"
sed -e "s|include \"$NFT_DIR/|include \"$scratch$NFT_DIR/|" "$newmain" > "$scratch/nftables.conf"
nft -c -f "$scratch/nftables.conf" || { echo "nft -c -f failed on the assembled boot config; nothing changed"; exit 1; }
echo "  nft -c -f (future $NFT_MAIN with the new files): ok"

# --- the live delta transaction: drop tagged rules, re-create the table, insert the accepts ---------
tx=$(mktemp)
nft -a list ruleset 2>/dev/null | awk -v tag="$TAG" '
  /^table / { fam=$2; tbl=$3 } /^\tchain / { ch=$2 }
  index($0, tag) && match($0, /# handle [0-9]+$/) { print "delete rule " fam " " tbl " " ch " handle " substr($0, RSTART+9) }' > "$tx"
gen_hub_nft "$scratch$ELEM" | grep -v '^#!' >> "$tx"
for (( i=${#INS_CONF[@]}-1; i>=0; i-- )); do printf 'insert rule %s %s %s\n' "${INS_CONF[$i]%%|*}" "${INS_CONF[$i]#*|}" "$TAG" >> "$tx"; done
for (( i=${#INS_LIVE[@]}-1; i>=0; i-- )); do printf 'insert rule %s %s %s\n' "${INS_LIVE[$i]%%|*}" "${INS_LIVE[$i]#*|}" "$TAG" >> "$tx"; done
nft -c -f "$tx" || { echo "nft -c -f failed on the live transaction; nothing changed"; exit 1; }
echo "  live transaction (checked): re-create table inet jobboard_hub, plus:"
grep -E '^(delete rule|insert rule)' "$tx" | sed 's/^/      /' || true
[ -s "$tx" ] && grep -qE '^(delete rule|insert rule)' "$tx" || echo "      (no tagged rules to delete, no inserts)"

log "files${CHECK_NOTE}"
backup
run mkdir -p "$CONF_DIR" "$NFT_DIR"
install_content "$ELEM" 0644 < "$scratch$ELEM"        # first: the fresh ranges, so the table below never loads empty sets
gen_hub_nft "$ELEM" | install_content "$HUB_NFT" 0644
install_content "$NFT_MAIN" 0755 < "$newmain"
gen_accepts | install_content "$ACCEPTS_BIN" 0755
install_content /etc/systemd/system/jobboard-hub-accepts.service 0644 <<EOF
# GENERATED by deploy/hub-bootstrap.sh: see $ACCEPTS_BIN
[Unit]
Description=jobboard-hub: accept rules in Docker's drop-policy forward chain
After=nftables.service docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$ACCEPTS_BIN
[Install]
WantedBy=multi-user.target
EOF
gen_cfips | install_content "$CFIPS_BIN" 0755
install_content /etc/systemd/system/jobboard-cloudflare-ips.service 0644 <<EOF
# GENERATED by deploy/hub-bootstrap.sh: see $CFIPS_BIN
[Unit]
Description=jobboard-hub: refresh Cloudflare IP ranges in nftables
After=network-online.target nftables.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$CFIPS_BIN
EOF
install_content /etc/systemd/system/jobboard-cloudflare-ips.timer 0644 <<'EOF'
# GENERATED by deploy/hub-bootstrap.sh
[Unit]
Description=jobboard-hub: weekly Cloudflare IP range refresh
[Timer]
OnCalendar=weekly
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
install_content /etc/sysctl.d/90-jobboard-hub.conf 0644 <<'EOF'
# jobboard-hub (deploy/hub-bootstrap.sh): forward the DNAT'd public flows and the laptop's traffic over wg0
net.ipv4.ip_forward = 1
EOF

log "apply${CHECK_NOTE}"
if [ "$CHECK" = 1 ]; then
  plan "order: fetch Cloudflare ranges (done above, temp file) -> backup -> write $ELEM -> other files -> live transaction"
  plan "sysctl -w net.ipv4.ip_forward=1"
  plan "nft -f <live transaction above>   # the table loads with the fetched sets in the same transaction: 443 never closes"
  plan "systemctl daemon-reload; enable nftables (boot persistence only, NOT restarted); enable jobboard-hub-accepts; enable --now jobboard-cloudflare-ips.timer (weekly refresh)"
  echo; echo "dry run complete; run without --check to apply."
  exit 0
fi
sysctl -q -w net.ipv4.ip_forward=1
nft -f "$tx" && echo "  live rules applied (sets populated in the same transaction)"
systemctl daemon-reload
systemctl is-enabled -q nftables 2>/dev/null || systemctl enable -q nftables && echo "  nftables.service enabled for boot (not restarted)"
systemctl enable -q jobboard-hub-accepts.service
systemctl enable -q --now jobboard-cloudflare-ips.timer

log "state"
echo "  table inet jobboard_hub:"; nft list table inet jobboard_hub | sed 's/^/      /'
N4=$(nft list set inet jobboard_hub cloudflare_v4 2>/dev/null | grep -c '/' || true)
echo "  cloudflare_v4 entries: ${N4:-0}   ip_forward: $(sysctl -n net.ipv4.ip_forward)   443 DNAT: $([ "$DNAT_ON" = 1 ] && echo ENABLED || echo disabled)"
echo "  verify from the laptop: ssh sldesktop (2222 forward) and ssh uscentral1 still work; ping 10.88.0.2 over wg0;"
echo "  after enable-dnat: curl -sI https://jobs.slindskog.net/login.html  (served by the desktop: check its nginx access log)"
echo "  rollback: $0 disable-dnat   (443 back on the hub)   |   boot config backup: $BACKUP"
