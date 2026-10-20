#!/usr/bin/env bash
# End-to-end smoke test against a running stack with auth ON (dev: ./dev.sh up-auth; CI: the same stack).
#   tests/smoke.sh <web-url> <mcp-url> <password>
#   tests/smoke.sh http://127.0.0.1:8081 http://127.0.0.1:8091 dev
# Checks the credential gate (anonymous, forged, cookie, token), the cross-site guard, login throttling
# wiring, the MCP transport, the geographic scan's endpoints (a run with no finder enabled, so no
# network), and a job + resume round trip. Needs curl and python3. Idempotent: reuses the "Smoke test
# job" and the identical resume bytes on re-runs (each run adds one empty geo run to the history).
set -euo pipefail
WEB=${1:?web url}; MCP=${2:?mcp url}; PW=${3:?password}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT; TOKEN=
CJ=$TMP/cookies
JSON=(-H 'Content-Type: application/json')
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
expect() {  # expect <code> <curl args...>
  local want=$1; shift; local got; got=$(code "$@")
  local shown="$*"; shown=${shown//${TOKEN:-__none__}/<token>}; shown=${shown//$TMP/<tmp>}
  if [ "$got" != "$want" ]; then echo "FAIL: want $want got $got: curl $shown"; exit 1; fi
  printf 'ok %s  %s\n' "$want" "$shown"
}
jq() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

echo "== waiting for $WEB"
for i in $(seq 1 60); do [ "$(code "$WEB/login.html")" = 200 ] && break; sleep 1; done
expect 200 "$WEB/login.html"

echo "== anonymous: only the login paths answer"
expect 401 "$WEB/api/stats"
expect 401 "$WEB/api/stats" -H 'Authorization: Bearer nope'
expect 401 "$WEB/api/stats" -H 'Cookie: jobboard_session=1794518789.0123456789abcdef.0000000000000000000000000000000000000000000000000000000000000000'
expect 302 "$WEB/"
expect 302 "$WEB/view.html?id=1"
expect 302 -I "$WEB/index.html"
expect 302 "$WEB/files/1/x.pdf"
expect 401 -X POST "$MCP/mcp" "${JSON[@]}" -d '{}'
expect 401 "$MCP/mcp"
expect 401 -X DELETE "$MCP/mcp"
expect 401 -X POST "$WEB/api/auth/login" "${JSON[@]}" -d '{"password":"wrong"}'
expect 403 -X POST "$WEB/api/auth/token" "${JSON[@]}" -d '{"name":"smoke","password":"wrong"}'

echo "== token"
TOKEN=$(curl -sf -X POST "$WEB/api/auth/token" "${JSON[@]}" -d "{\"name\":\"smoke\",\"password\":\"$PW\"}" | jq 'd["token"]')
AUTH=(-H "Authorization: Bearer $TOKEN")
expect 200 "$WEB/api/stats" "${AUTH[@]}"
curl -sf "$WEB/api/stats" "${AUTH[@]}" | jq 'd["auth"]' | grep -qx True || { echo "FAIL: /api/stats auth flag"; exit 1; }
expect 200 "$WEB/api/auth/me" "${AUTH[@]}"

echo "== session cookie"
expect 200 -c "$CJ" -X POST "$WEB/api/auth/login" "${JSON[@]}" -d "{\"password\":\"$PW\"}"
expect 200 -b "$CJ" "$WEB/"
expect 200 -b "$CJ" -I "$WEB/index.html"
expect 200 -b "$CJ" "$WEB/api/jobs"
expect 403 -b "$CJ" -X POST "$WEB/api/auth/logout" -H 'Origin: https://evil.example'   # cross-site POST with a cookie
expect 401 -b "$CJ" -X POST "$MCP/mcp" "${JSON[@]}" -d '{}'                               # cookies mean nothing to MCP

echo "== MCP"
expect 200 -X POST "$MCP/mcp" "${AUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
curl -sf -X POST "$MCP/mcp" "${AUTH[@]}" "${JSON[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_jobs","arguments":{"limit":1}}}' \
  | jq 'd["result"]["isError"]' | grep -qx False || { echo "FAIL: mcp list_jobs"; exit 1; }
echo "ok mcp tools/call"

echo "== events (server log + run updates): JSON and the SSE stream"
expect 401 "$WEB/api/events"
expect 401 "$WEB/api/events/stream"
expect 200 "$WEB/api/events?since=0" "${AUTH[@]}"
curl -sf "$WEB/api/events?since=0" "${AUTH[@]}" | jq 'any(e["kind"]=="log" for e in d["events"])' | grep -qx True || { echo "FAIL: /api/events has no log events"; exit 1; }
STREAM=$(curl -s -N --max-time 2 "$WEB/api/events/stream" "${AUTH[@]}" || true)     # times out by design; the opening comment must have arrived
printf '%s' "$STREAM" | head -1 | grep -q '^: connected' || { echo "FAIL: event stream did not open: $STREAM"; exit 1; }
echo "ok event stream"
expect 404 -X POST "$WEB/api/runs/999999/cancel" "${AUTH[@]}" "${JSON[@]}" -d '{}'

echo "== scan history: the runs list, repeat of an unknown run, the Scans page behind the gate"
expect 200 "$WEB/api/runs?kind=scan&limit=5" "${AUTH[@]}"
curl -sf "$WEB/api/runs?kind=scan&limit=5" "${AUTH[@]}" | jq 'all(r["kind"]=="scan" and not isinstance(r.get("args"), str) for r in d["runs"])' | grep -qx True || { echo "FAIL: /api/runs kind filter or parsed args"; exit 1; }
expect 404 -X POST "$WEB/api/runs/999999/repeat" "${AUTH[@]}" "${JSON[@]}" -d '{}'
expect 302 "$WEB/scans.html"
expect 200 -b "$CJ" "$WEB/scans.html"
expect 200 -b "$CJ" "$WEB/common.js"
expect 200 -b "$CJ" "$WEB/geo.js"

echo "== geographic scan: the registry, centre parsing, places behind the gate, a run with no finder enabled"
expect 401 "$WEB/api/geo/categories"
expect 401 "$WEB/api/places"
expect 401 -X POST "$WEB/api/geo/scan" "${JSON[@]}" -d '{"center":"35.08,-106.65"}'
expect 200 "$WEB/api/geo/categories" "${AUTH[@]}"
curl -sf "$WEB/api/geo/categories" "${AUTH[@]}" | jq 'any(c["key"]=="government" for c in d["categories"]) and "osm" in [f["name"] for f in d["finders"]]' | grep -qx True || { echo "FAIL: /api/geo/categories"; exit 1; }
expect 200 "$WEB/api/geo/center?center=35.08,-106.65" "${AUTH[@]}"
curl -sf "$WEB/api/geo/center?center=35.08,-106.65" "${AUTH[@]}" | jq 'abs(d["lat"]-35.08)<1e-9 and abs(d["lon"]+106.65)<1e-9' | grep -qx True || { echo "FAIL: /api/geo/center"; exit 1; }
expect 400 -X POST "$WEB/api/geo/scan" "${AUTH[@]}" "${JSON[@]}" -d '{"center":"35.08,-106.65","categories":["nope"]}'
expect 200 "$WEB/api/places?limit=1" "${AUTH[@]}"
expect 404 "$WEB/api/places/999999999" "${AUTH[@]}"
OUT=$(curl -s -w '\n%{http_code}' -X POST "$WEB/api/geo/scan" "${AUTH[@]}" "${JSON[@]}" -d '{"center":"35.0844, -106.6504","finders":[]}')
CODE=${OUT##*$'\n'}; BODY=${OUT%$'\n'*}
[ "$CODE" = 202 ] || { echo "FAIL: POST /api/geo/scan: $CODE $BODY"; exit 1; }
RID=$(printf '%s' "$BODY" | jq 'd["run_id"]')
echo "ok 202 geo run $RID started"
ST=running
for i in $(seq 1 90); do
  ST=$(curl -sf "$WEB/api/runs/$RID" "${AUTH[@]}" | jq 'd["status"]'); [ "$ST" != running ] && break; sleep 1
done
[ "$ST" = done ] || { echo "FAIL: geo run $RID ended '$ST'"; curl -s "$WEB/api/runs/$RID" "${AUTH[@]}" | jq 'd.get("log")'; exit 1; }
curl -sf "$WEB/api/runs/$RID" "${AUTH[@]}" | jq 'd["kind"]=="geo" and json.loads(d["summary"])["discovered"]==0 and json.loads(d["summary"])["boards_scanned"]==0' | grep -qx True || { echo "FAIL: geo run summary"; curl -s "$WEB/api/runs/$RID" "${AUTH[@]}"; exit 1; }
curl -sf "$WEB/api/runs?kind=geo&limit=1" "${AUTH[@]}" | jq 'd["runs"][0]["request"]["op"]=="geo_scan" and d["runs"][0]["args"]["lat"]==35.0844 and d["runs"][0]["args"]["finders"]==[]' | grep -qx True || { echo "FAIL: geo run listed with its request"; exit 1; }
echo "ok geo run $RID done: 0 places, 0 boards"

echo "== job + resume round trip"
JID=$(curl -sf "$WEB/api/jobs?q=Smoke+test+job" "${AUTH[@]}" | jq '(d["jobs"] or [{}])[0].get("id","")')
if [ -z "$JID" ]; then
  curl -sf -X POST "$WEB/api/jobs" "${AUTH[@]}" "${JSON[@]}" -d '{"title":"Smoke test job","text":"Qualifications\nA degree."}' >/dev/null
  JID=$(curl -sf "$WEB/api/jobs?q=Smoke+test+job" "${AUTH[@]}" | jq 'd["jobs"][0]["id"]')
fi
echo "job id $JID"
upload() {  # upload <filename> <marker> -> "<id> <stored>"
  local b64; b64=$(printf '%%PDF-1.4 %s' "$2" | base64 | tr -d '\n')
  curl -sf -X POST "$MCP/mcp" "${AUTH[@]}" "${JSON[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"upload_resume\",\"arguments\":{\"job_id\":$JID,\"filename\":\"$1\",\"content_base64\":\"$b64\",\"label\":\"smoke\"}}}" \
    | python3 -c 'import sys,json; r=json.load(sys.stdin)["result"]; assert not r["isError"], r; d=json.loads(r["content"][0]["text"]); assert d["url"].startswith("/files/job/"), d; print(d["id"], d["stored"])'
}
FILE="$WEB/files/job/$JID/smoke.pdf"
MARK="smoke $(date +%s)"
read -r ID1 S1 <<<"$(upload smoke.pdf "$MARK v1")"
read -r ID2 S2 <<<"$(upload smoke.pdf "$MARK v2")"
read -r ID3 S3 <<<"$(upload smoke.pdf "$MARK v2")"
[ "$ID1" = "$ID2" ] && [ "$ID2" = "$ID3" ] || { echo "FAIL: re-upload changed the file id ($ID1 $ID2 $ID3)"; exit 1; }
[ "$S2" = replaced ] && [ "$S3" = unchanged ] || { echo "FAIL: stored flags were $S1/$S2/$S3, want */replaced/unchanged"; exit 1; }
expect 302 "$FILE"
curl -sf "$FILE" "${AUTH[@]}" | grep -q "$MARK v2" || { echo "FAIL: file URL did not serve the replaced bytes"; exit 1; }
curl -sf -D - -o /dev/null "$FILE" "${AUTH[@]}" | grep -qi '^cache-control: no-store' || { echo "FAIL: resume response is cacheable"; exit 1; }
N=$(curl -sf "$WEB/api/jobs/$JID/resumes" "${AUTH[@]}" | jq 'len([r for r in d["resumes"] if r["filename"]=="smoke.pdf"])')
[ "$N" = 1 ] || { echo "FAIL: $N rows for smoke.pdf, want 1"; exit 1; }
curl -sf "$WEB/api/jobs/$JID/resumes" "${AUTH[@]}" | jq '[r["url"] for r in d["resumes"] if r["filename"]=="smoke.pdf"][0]' | grep -qx "/files/job/$JID/smoke.pdf" || { echo "FAIL: list_resumes url"; exit 1; }
expect 404 "$WEB/files/$ID1/smoke.pdf" "${AUTH[@]}"
echo "ok resume round trip: one row (id $ID1), re-upload replaced in place, no-store, old id-style URL gone"
echo "smoke ok"
