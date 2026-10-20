#!/usr/bin/env bash
# WireGuard watchdog for the desktop. Installed by deploy/bootstrap.sh as /usr/local/sbin/jobboard-wg-watch
# and run every 2 minutes by jobboard-wg-watch.timer. Usage: jobboard-wg-watch <hub-ip> <wg-interface>
#
# Why: every inbound path to this host (Cloudflare 443, GitHub Actions ssh via the hub's 2222 forward, the
# laptop's ssh and MCP) is a flow the HUB initiates over the tunnel. The desktop sits behind a home NAT, so
# the hub can only reach it while the desktop's PersistentKeepalive keeps a NAT mapping open; if the
# endpoint goes stale (ISP re-addressing, NAT table reset, suspend) nothing on the hub side can recover
# it. Pinging the hub detects that, and restarting wg-quick@wg0 re-handshakes from our side.
# Three consecutive failing runs (3 pings each) before restarting keeps a single lost packet harmless.
# State lives under /run (cleared at boot); output goes to the journal through the service.
set -u
HUB_IP="${1:?hub ip}"; WG_IF="${2:?wg interface}"
STATE=/run/jobboard-wg-watch
mkdir -p "$STATE"
if ping -q -c 3 -W 2 -I "$WG_IF" "$HUB_IP" >/dev/null 2>&1; then
  if [ -s "$STATE/failures" ] && [ "$(cat "$STATE/failures")" != 0 ]; then echo "hub $HUB_IP reachable again"; fi
  echo 0 > "$STATE/failures"
  exit 0
fi
n=$(( $(cat "$STATE/failures" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE/failures"
if [ "$n" -lt 3 ]; then
  echo "hub $HUB_IP unreachable over $WG_IF ($n/3)"
  exit 0
fi
echo "hub $HUB_IP unreachable for $n runs; restarting wg-quick@$WG_IF (last handshake: $(wg show "$WG_IF" latest-handshakes 2>/dev/null | awk '{print $2}' | head -1 || echo '?'))"
systemctl restart "wg-quick@$WG_IF" && echo "wg-quick@$WG_IF restarted" || echo "restart of wg-quick@$WG_IF failed"
echo 0 > "$STATE/failures"
