#!/usr/bin/env bash
# jobboard-egress apply|remove|verify|status — policy routing that sends every container's internet traffic
# through the WireGuard hub and refuses it while the tunnel is down. Installed by deploy/bootstrap.sh (egress
# mode) as /usr/local/sbin/jobboard-egress with the @PLACEHOLDERS@ substituted; run by jobboard-egress.service
# (ExecStart apply, ExecStop remove) and directly by the bootstrap on a live host.
#
# Rules for CONTAINER_NETS, the /23 holding every bridge of ours (app stack 172.31.1.0/24, crawl 172.31.0.0/24):
#   pref 98   from NETS to NETS     lookup main   bridge-to-bridge packets stay on the host, where Docker's
#                                                 isolation decides (never tunnelled towards a private address)
#   pref 99   from NETS to WG_NET   lookup main   the tunnel subnet via the connected route (hub, laptop)
#   pref 100  from NETS             lookup 100    table 100 holds only `default dev wg0` (wg-quick PostUp)
#   pref 101  from NETS             prohibit      table 100 empty (tunnel down) -> refused. The kill switch:
#                                                 container traffic never falls through to the LAN default route.
# Docker's own MASQUERADE then rewrites the source to the wg0 address (10.88.0.2) and the hub masquerades that
# out of eth0, so the exit address is the hub's. The host itself keeps its LAN default route (wg0.conf has
# Table = off; only AllowedIPs routes are skipped, the connected 10.88.0.0/24 route still comes from Address).
# rp_filter is loose (2): replies for 10.88.0.2 arrive on wg0 while the main table would route their sources
# via the LAN; strict filtering would drop them. Table 100 itself belongs to wg-quick (PostUp/PostDown).
#
# apply reconciles BY CONTENT, pref by pref: a rule at one of these prefs whose text differs (an older subnet)
# is deleted and the wanted one added if missing, so a changed rule set takes effect live and re-runs are
# quiet. remove deletes whatever sits at these prefs. verify prints them and exits 1 on any drift.
set -euo pipefail
NETS=@CONTAINER_NETS@
WG_NET=@WG_NET@
WG_IF=@WG_IF@
TABLE=100
RULES=(
  "98 from $NETS to $NETS lookup main"
  "99 from $NETS to $WG_NET lookup main"
  "100 from $NETS lookup $TABLE"
  "101 from $NETS prohibit"
)

# rules_at PREF: the live rules at PREF, normalised to the "SELECTOR ACTION" text after the "PREF:" prefix.
rules_at() {
  local pref=$1 line words
  ip -4 rule show | while IFS= read -r line; do
    [[ $line == "$pref:"* ]] || continue
    line=${line#"$pref:"}
    read -ra words <<< "${line//$'\t'/ }"
    printf '%s\n' "${words[*]}"
  done
}
del_rule() {   # PREF SPEC: delete that one rule; if its text does not re-parse, delete by pref alone
  # shellcheck disable=SC2086  # the spec is a deliberate word list
  ip -4 rule del $2 pref "$1" 2>/dev/null || ip -4 rule del pref "$1"
}
reconcile() {  # PREF SPEC: make PREF hold exactly one rule reading SPEC
  local pref=$1 spec=$2 line have=0
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    if [[ $line == "$spec" && $have -eq 0 ]]; then have=1; echo "present: pref $pref: $spec"
    else del_rule "$pref" "$line"; echo "replaced: pref $pref: ($line) -> ($spec)"; fi
  done < <(rules_at "$pref")
  # shellcheck disable=SC2086
  (( have )) || { ip -4 rule add $spec pref "$pref"; echo "added:   pref $pref: $spec"; }
}
apply() {
  local entry
  for entry in "${RULES[@]}"; do reconcile "${entry%% *}" "${entry#* }"; done
  sysctl -q -w net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2
  if [[ -d /sys/class/net/$WG_IF ]]; then sysctl -q -w "net.ipv4.conf.$WG_IF.rp_filter=2"
  else echo "$WG_IF absent now; /etc/sysctl.d/90-jobboard-egress.conf sets its rp_filter when it appears"; fi
  echo "egress policy applied ($NETS -> table $TABLE via $WG_IF, prohibit when empty)"
}
remove() {
  local entry pref line
  for entry in "${RULES[@]}"; do
    pref=${entry%% *}
    while IFS= read -r line; do [[ -n $line ]] && { del_rule "$pref" "$line"; echo "removed: pref $pref: $line"; }; done < <(rules_at "$pref")
  done
  echo "egress policy removed (table $TABLE is wg-quick's: PostDown flushes it)"
}
verify() {
  local entry pref spec rc=0 lines
  for entry in "${RULES[@]}"; do
    pref=${entry%% *}; spec=${entry#* }
    mapfile -t lines < <(rules_at "$pref")
    if (( ${#lines[@]} == 1 )) && [[ ${lines[0]} == "$spec" ]]; then echo "ok:      $pref: $spec"
    elif (( ${#lines[@]} == 0 )); then echo "MISSING: $pref: want ($spec)"; rc=1
    else printf 'DRIFT:   %s: want (%s), have (%s)\n' "$pref" "$spec" "${lines[*]}"; rc=1; fi
  done
  if ip -4 route show table "$TABLE" 2>/dev/null | grep -q "^default dev $WG_IF"; then echo "ok:      table $TABLE: default dev $WG_IF"
  else echo "NOTE:    table $TABLE has no default via $WG_IF: tunnel down or PostUp not run -> containers are refused (kill switch)"; fi
  (( rc == 0 )) && echo "egress policy verified" || echo "egress policy DIFFERS: run jobboard-egress apply" >&2
  return "$rc"
}
status() {
  echo "== ip rule (prefs 98-101)"; ip -4 rule show | grep -E '^(98|99|100|101):' || echo "(none)"
  echo "== ip route table $TABLE (default dev $WG_IF while the tunnel is up)"; ip -4 route show table "$TABLE" 2>/dev/null || true
  echo "== rp_filter"; sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.default.rp_filter 2>/dev/null
  [[ -d /sys/class/net/$WG_IF ]] && sysctl "net.ipv4.conf.$WG_IF.rp_filter"
  return 0
}
case "${1:-status}" in
  apply) apply ;; remove) remove ;; verify) verify ;; status) status ;;
  *) echo "usage: $0 apply|remove|verify|status" >&2; exit 2 ;;
esac
