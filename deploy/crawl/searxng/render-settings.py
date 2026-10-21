#!/usr/bin/env python3
"""Render SearXNG's settings.yml from the template with the app's proxy list.

    render-settings.py <template settings.yml> <app .env> <jobscan dir>   -> rendered YAML on stdout

settings.yml cannot read environment variables, and SearXNG's `outgoing.proxies` (a list is used round
robin, one proxy per request) is the only way to spread its engine queries over several exit addresses.
The list is built from the same JOBSCAN_PROXY / JOBSCAN_PROXY_COUNTRIES the app uses, with the app's own
parser (jobscan/browser.py parse_proxies: http://user:pass@host:port, Decodo's host:port:user:pass lines,
or one gateway URL with {session} / {country}). A single {session} template becomes SESSIONS entries with
distinct, stable session ids (derived from the template, so re-renders do not churn the file), the exit
countries cycled over them as ProxyPool does per level. The line `# @OUTGOING_PROXIES@` in the template is
replaced by the list, or removed when JOBSCAN_PROXY is empty (SearXNG goes direct).
Run by deploy/bootstrap.sh; stdlib only, so it runs on the host (browser.py imports nothing else at module level).
"""
import hashlib
import sys

SESSIONS = 8


def read_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def proxy_urls(env):
    import browser  # jobscan/browser.py (sys.path set by main)

    spec = env.get("JOBSCAN_PROXY", "")
    countries = [c.strip().lower() for c in env.get("JOBSCAN_PROXY_COUNTRIES", "us,ca").split(",") if c.strip()]
    urls = browser.parse_proxies(spec, countries)
    if len(urls) == 1 and "{session}" in urls[0]:
        tpl = urls[0]
        out = []
        for i in range(SESSIONS):
            sid = hashlib.sha256(f"{tpl}#{i}".encode()).hexdigest()[:8]
            u = tpl.replace("{session}", sid)
            # parse_proxies already targeted the first country; with_country re-targets (it normalises an
            # already-targeted username), so the sessions cycle through the list like the app's levels do.
            if countries:
                u = browser.with_country(u, countries[i % len(countries)])
            out.append(u)
        return out
    return urls


def render(template, urls):
    out = []
    for line in open(template, encoding="utf-8"):
        if line.strip() == "# @OUTGOING_PROXIES@":
            if urls:
                out.append("  proxies:                      # rendered by deploy/bootstrap.sh from JOBSCAN_PROXY: round robin, one proxy per request\n")
                out.append("    all://:\n")
                out.extend(f"      - {u}\n" for u in urls)
            continue
        out.append(line)
    return "".join(out)


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    template, envfile, jobscan_dir = sys.argv[1:4]
    sys.path.insert(0, jobscan_dir)
    env = read_env(envfile)
    urls = proxy_urls(env)
    sys.stdout.write(render(template, urls))
    print(f"searxng outgoing proxies: {len(urls)}", file=sys.stderr)


if __name__ == "__main__":
    main()
