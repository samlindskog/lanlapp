# crawl — self-hosted Firecrawl + SearXNG beside the app

A second compose project (`-p crawl`) on the desktop that gives the app a rendering crawler and a
meta-search engine without any public exposure. It ships through the bootstrap (`deploy/bootstrap.sh`,
`CRAWL=1`), not through a push: a deploy rebuilds the app only.

```
network crawl (external, fixed subnet, created once: docker network create --subnet 172.31.0.0/24 crawl)
  jobboard          the app container (jobboard/compose.prod.yaml joins `crawl` next to its own network)
                      FIRECRAWL_URL=http://firecrawl-api:3002   SEARXNG_URL=http://searxng:8080
                      JOBSCAN_RELAY_PORT=8899  -> the app's proxy relay, used by Firecrawl as PROXY_SERVER
                      JOBSCAN_RELAY_PEERS=172.31.0.0/24 -> the relay accepts loopback plus these CIDRs only,
                      which is why the network's subnet is fixed (bootstrap, compose-up.sh and dev.sh create it so;
                      an existing `crawl` network with another subnet is kept with a warning and must match)
  firecrawl-api     ghcr.io/firecrawl/firecrawl:2.11.560  API + workers in one container (harness.js --start-docker)
  playwright-service ghcr.io/firecrawl/playwright-service  headless Chromium renderer for the API (BLOCK_MEDIA=true)
  redis             cache + rate limits (no persistence)
  rabbitmq          Firecrawl's job queue transport
  nuq-postgres      Firecrawl's NuQ queue backend (volume nuq-pgdata)
  searxng           searxng/searxng, settings from ./searxng/settings.yml (formats html+json, limiter off, six web
                      engines, short suspensions), rendered by the bootstrap with the app's proxy list (see below)
```

No service publishes a port. The only clients are containers on `crawl`: the app calls Firecrawl and
SearXNG by service name, Firecrawl calls SearXNG (`SEARXNG_ENDPOINT`) for `/v2/search`, and both Firecrawl
and Playwright send their outbound requests through the app's relay (`PROXY_SERVER=http://jobboard:8899`),
so the app's one egress policy (direct first, Decodo on block) applies to crawled pages too. The relay only
serves peers inside `JOBSCAN_RELAY_PEERS` (the `crawl` subnet) and loopback, so nothing else on the host can
borrow the app's proxy credentials.

## Environment

The stack is started with the app's env file (`--env-file /opt/jobboard/.env`); `.env.example` lists the
keys it reads: `OPENAI_API_KEY` (shared with the app; Firecrawl's JSON extraction), `SEARXNG_SECRET`
(the bootstrap appends one), and optional `FIRECRAWL_*` tuning. `USE_DB_AUTHENTICATION=false`: the API
is unauthenticated and must stay unreachable from outside the network, which the missing `ports:` and the
desktop firewall (nothing on the LAN side is filtered, but nothing is published either) guarantee.

Concurrency is deliberately low (2 workers, 2 jobs, 3 pages) and every service has a `mem_limit`
(about 5.3 GB in all on top of the app's 3 GB); raise `FIRECRAWL_*` in `.env` if the desktop has room.

## SearXNG under load

Search engines throttle a single exit address after a few hundred queries (brave and google answer "too
many requests", duckduckgo serves a CAPTCHA), and SearXNG then suspends the engine; in a JSON reply this
shows as `unresponsive_engines`. `searxng/settings.yml` is tuned for that: six web engines queried in
parallel (duckduckgo, bing, brave, qwant, startpage, mojeek; google's scraper off), the encyclopaedia and
utility engines off, 6 s per engine, and suspensions of 1 to 5 minutes instead of hours or days.

Optionally the queries are spread over several exit addresses: `outgoing.proxies` takes a list that SearXNG
uses round robin. Because settings.yml cannot read environment variables, the bootstrap renders the template
(`render-settings.py`) with the app's `JOBSCAN_PROXY` / `JOBSCAN_PROXY_COUNTRIES` from `.env`, using the
app's own parser (`jobscan/browser.py parse_proxies`: Decodo's `host:port:user:pass` list, `http://` URLs,
or one `{session}` gateway URL, which becomes 8 entries with distinct session ids), writes the result to
`/etc/jobboard/searxng-settings.yml` (0600, owned by the container's uid 977: it holds the proxy password)
and records `SEARXNG_SETTINGS=/etc/jobboard/searxng-settings.yml` in `.env`, which compose substitutes into
the volume line. A changed file means a forced recreate of the searxng container (a bind-mounted file keeps
its old inode otherwise). With `JOBSCAN_PROXY` empty the placeholder line is dropped and SearXNG goes direct.
Dev (`dev.sh up-fc`) mounts the template unchanged.

## Operations

```
cd /opt/jobboard
docker compose -f deploy/crawl/compose.yaml --env-file .env -p crawl up -d        # start / apply changes
docker compose -p crawl ps                                                        # state
docker compose -p crawl logs -f --tail 100 firecrawl-api                          # logs
docker compose -p crawl down                                                      # stop (volumes kept)
docker run --rm --network crawl curlimages/curl -s -X POST http://firecrawl-api:3002/v2/scrape \
   -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'         # probe from inside the network
docker run --rm --network crawl curlimages/curl -s 'http://searxng:8080/search?q=test&format=json' | head -c 300
```

Dev: `cd jobboard && ./dev.sh up-fc` creates the network, starts this project, then the dev stack with
`compose.crawl.yaml` (same env, same network). `./dev.sh down` tears both down.

## Upgrading

Pin new tags here after reading the matching `docker-compose.yaml` in github.com/firecrawl/firecrawl:
the service set and variable names change between releases (2.11 added RabbitMQ and NuQ Postgres; a
FoundationDB backend exists behind `NUQ_BACKEND=fdb` and is not used here). `playwright-service` and
`nuq-postgres` publish only `latest` and `sha-*` tags, so those are pinned by sha.
