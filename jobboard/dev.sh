#!/usr/bin/env bash
# Local dev stack helper (run from anywhere).
#   ./dev.sh up            app + db, no auth            -> http://127.0.0.1:8080  MCP http://127.0.0.1:8090/mcp
#   ./dev.sh up-auth       + auth on + nginx in front   -> http://127.0.0.1:8081  MCP http://127.0.0.1:8091/mcp (password: dev)
#   ./dev.sh up-fc         app + db + the crawl stack (deploy/crawl: Firecrawl + SearXNG) on network `crawl`
#   ./dev.sh smoke         tests/smoke.sh through nginx (needs up-auth)
#   ./dev.sh token [name]  mint a bearer token in the dev database (auth stack)
#   ./dev.sh logs [svc]    follow logs;  ./dev.sh ps;  ./dev.sh down (also the crawl project when it exists)
set -euo pipefail
cd "$(dirname "$0")"
BASE=(docker compose -f compose.yaml)
AUTH=("${BASE[@]}" -f compose.auth.yaml)
FC=("${BASE[@]}" -f compose.crawl.yaml)
CRAWL=(docker compose -f ../deploy/crawl/compose.yaml -p crawl)
[ -f .env ] && CRAWL+=(--env-file .env)      # OPENAI_API_KEY / SEARXNG_SECRET, like the app reads them
export SEARXNG_SECRET="${SEARXNG_SECRET:-dev-searxng-secret-not-for-production}"
case "${1:-}" in
  up)      "${BASE[@]}" up -d --build --remove-orphans ;;
  up-auth) "${AUTH[@]}" up -d --build --remove-orphans ;;
  up-fc)   docker network inspect crawl >/dev/null 2>&1 || docker network create crawl >/dev/null
           "${CRAWL[@]}" up -d --remove-orphans
           "${FC[@]}" up -d --build --remove-orphans ;;
  down)    "${AUTH[@]}" down
           if [ -n "$(docker compose -p crawl ps -aq 2>/dev/null)" ]; then "${CRAWL[@]}" down; fi ;;
  ps)      "${AUTH[@]}" ps ;;
  logs)    "${AUTH[@]}" logs -f --tail 100 "${@:2}" ;;
  smoke)   exec ../tests/smoke.sh http://127.0.0.1:8081 http://127.0.0.1:8091 dev ;;
  token)   "${BASE[@]}" exec jobboard python3 server.py issue-token "${2:-dev}" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
