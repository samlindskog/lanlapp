#!/usr/bin/env bash
# End-to-end smoke test against a running stack with auth ON (dev: ./dev.sh up-auth; CI: the same stack).
#   tests/smoke.sh <web-url> <mcp-url> <password>
#   tests/smoke.sh http://127.0.0.1:8081 http://127.0.0.1:8091 dev
# Checks the credential gate (anonymous, forged, cookie, token), the cross-site guard, login throttling
# wiring, the MCP transport, and a job + resume round trip. Needs curl and python3. Idempotent: reuses
# the "Smoke test job" and the identical resume bytes on re-runs.
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
