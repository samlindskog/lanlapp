# Migration runbook: jobboard from the VPS (uscentral1) to the desktop (sldesktop)

Target: the desktop (Debian 13, LAN 10.0.0.14, WireGuard peer 10.88.0.2, sshd 1025) runs the stack;
the VPS (public 72.14.178.40, WireGuard hub 10.88.0.1) becomes a pure hub + forwarder (Cloudflare 443
DNAT to the desktop, the existing 2222 ssh DNAT, laptop routing). DNS does not change: jobs.slindskog.net
and slindskog.net keep pointing at Cloudflare, Cloudflare keeps the VPS's public address as origin.

ssh aliases used throughout (from the laptop, `~/.ssh/config`): `uscentral1` = hub, `sldesktoplocal` = desktop
over the LAN (10.0.0.14:1025), `sldesktop` = desktop through the hub's 2222 forward. Every remote path is
absolute (`~` would expand on the Mac). Staging directory on both hosts: `/home/sam/jobboard-bootstrap/`.

Every step says how to roll it back. Steps 1 to 5 change nothing the user sees; step 6 is the cutover.

## 0. Before anything

```
git fetch origin && git log --oneline -3 origin/next          # the branch the desktop will run
ls ~/.ssh/jobboard_deploy ~/.config/jobboard/token             # deploy key, Claude's token
```

The branch `next` holds all of this; the desktop deploys `origin/next` until cutover (a staged `deploy-ref`
file overrides the bootstrap's default `origin/master`), then `master` once `next` is merged and the file
is deleted.

## 1. Read before write (changes nothing)

```
scp deploy/bootstrap.sh uscentral1:/home/sam/jobboard-bootstrap/bootstrap.sh
ssh -t uscentral1 'touch /home/sam/jobboard-bootstrap/show-firewall && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh' | tee /tmp/hub-firewall.txt
scp deploy/hub-bootstrap.sh uscentral1:/home/sam/jobboard-bootstrap/hub-bootstrap.sh
ssh -t uscentral1 'sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh --check' | tee /tmp/hub-plan.txt

ssh -t uscentral1 'sudo cat /etc/wireguard/wg0.conf; sudo cat /etc/nginx/snippets/tls.conf /etc/nginx/snippets/cloudflare-realip.conf; ls -l /etc/nginx/sites-enabled'
ssh -t sldesktoplocal 'sudo cat /etc/wireguard/wg0.conf; ls -l /etc/nginx/sites-enabled 2>/dev/null; sudo nft list ruleset; ss -tlnp | grep -E ":(443|8090|1025|8080|8091) "; docker --version; docker compose version; free -g; df -h /var/lib/docker'
```

Check in the output:

- Hub: which table holds `dport 2222 ... dnat to 10.88.0.2:1025` (the plan must say "kept, not duplicated");
  every base chain with `policy drop` on the forward hook (the plan lists where it will insert accepts);
  whether `/etc/nftables.conf` has `flush ruleset` (then never `systemctl restart nftables` there); the paths of
  the Cloudflare origin cert, key and origin-pull CA in `snippets/tls.conf` (you copy them in step 3).
- Desktop: `wg0.conf` has `PersistentKeepalive = 25` on the hub peer and `AllowedIPs` covering 10.88.0.0/24;
  nothing already listens on 10.88.0.2:443 or :8090; no leftover drop-policy input chain; RAM: the app is
  limited to 3 GB and the crawl stack to about 5.3 GB.

Rollback: nothing to roll back.

## 2. Hermes teardown on the desktop and hub

Owned by the other project (sldesktop_nous). Finish it first: it removes the Hermes nftables table on the hub
and whatever Hermes left on the desktop (ports 9119/19999/8183, its nginx bits). Re-run step 1 afterwards and
confirm the 2222 DNAT still exists (if Hermes owned it, `hub-bootstrap.sh` will carry it instead: the plan
prints "jobboard_hub will carry it").

## 3. Desktop bootstrap (no public traffic yet)

Stage keys, certificates and secrets, then run the bootstrap. The certificates and the deploy user's key files
come from the hub through the bootstrap's `export-certs` mode (it reads the paths from the installed
`snippets/tls.conf`), travel through the laptop, and are deleted from the hub's staging directory afterwards:

```
mkdir -p /tmp/jobboard-cutover && chmod 700 /tmp/jobboard-cutover
ssh -t uscentral1 'touch /home/sam/jobboard-bootstrap/export-certs && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh'
scp 'uscentral1:/home/sam/jobboard-bootstrap/{origin.pem,origin.key,cloudflare-origin-pull-ca.pem,github_deploy_key,jobboard_deploy.pub}' /tmp/jobboard-cutover/
ssh uscentral1 'rm -f /home/sam/jobboard-bootstrap/{origin.pem,origin.key,cloudflare-origin-pull-ca.pem,github_deploy_key,jobboard_deploy.pub}'
ssh sldesktoplocal 'mkdir -p /home/sam/jobboard-bootstrap && chmod 700 /home/sam/jobboard-bootstrap'
scp /tmp/jobboard-cutover/* sldesktoplocal:/home/sam/jobboard-bootstrap/
WEB_PASSWORD='<the login password>' OPENAI_API_KEY='sk-...' deploy/push-secrets.sh sldesktoplocal   # writes secrets.env
ssh sldesktoplocal 'echo origin/next > /home/sam/jobboard-bootstrap/deploy-ref'   # deploy next until cutover (default: origin/master)
scp deploy/bootstrap.sh sldesktoplocal:/home/sam/jobboard-bootstrap/bootstrap.sh
ssh -t sldesktoplocal 'sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh'
```

(`jobboard_deploy.pub` must list Claude's and GitHub Actions' public keys, one per line; it is the hub's
`deploy_keys.pub`, so the same keys keep working.)

The bootstrap: requires docker; creates `deploy`; clones `/opt/jobboard` at `origin/next` (the `deploy-ref`
file; it prints the ref in use at the deploy step); writes `.env`;
installs nginx on 10.88.0.2:443 / :8090 with the certs; installs `table inet jobboard` (filters wg0 only);
enables the WireGuard watchdog; deploys; starts the crawl stack; mints `claude-mac` into
`/home/sam/jobboard-bootstrap/claude-token`.

Verify from the laptop (WireGuard on):

```
ssh sldesktoplocal 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/login.html; sudo nft list table inet jobboard; systemctl list-timers jobboard-wg-watch.timer --no-pager'
ssh sldesktop true                                   # the 2222 forward reaches the desktop
scp sldesktoplocal:/home/sam/jobboard-bootstrap/claude-token /tmp/desktop-token && ssh sldesktoplocal rm /home/sam/jobboard-bootstrap/claude-token
curl -s -H "Authorization: Bearer $(cat /tmp/desktop-token)" http://10.88.0.2:8090/mcp -X POST -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
curl -sk --resolve jobs.slindskog.net:443:10.88.0.2 https://jobs.slindskog.net/login.html -o /dev/null -w '%{http_code}\n'   # 400 expected: no client cert (origin pulls on)
claude mcp add --transport http jobboard-desktop http://10.88.0.2:8090/mcp --header "Authorization: Bearer $(cat /tmp/desktop-token)"
```

The last line registers the desktop under a second name so both can be compared; `stats` through
`jobboard-desktop` shows an empty database for now. The laptop reaches 10.88.0.2 through the hub's
wg0->wg0 forward; if ssh/curl hang here, the hub's forward chain is the first suspect (step 5 adds the rules;
until then the hub's current policy must already allow it, as it did for Hermes).

Rollback: `ssh -t sldesktoplocal 'cd /opt/jobboard && sudo docker compose -f jobboard/compose.prod.yaml --env-file .env down'`,
`sudo nft delete table inet jobboard`, `sudo rm /etc/nginx/sites-enabled/jobboard.conf && sudo systemctl reload nginx`,
`sudo systemctl disable --now jobboard-wg-watch.timer`. Nothing public changed.

## 4. Crawl stack

Started by step 3 (`CRAWL=1`). Check it, then probe from inside the network:

```
ssh sldesktoplocal 'docker compose -p crawl ps; docker run --rm --network crawl curlimages/curl -s "http://searxng:8080/search?q=test&format=json" | head -c 200; echo; docker run --rm --network crawl curlimages/curl -s -X POST http://firecrawl-api:3002/v2/scrape -H "Content-Type: application/json" -d "{\"url\":\"https://example.com\"}" | head -c 300'
```

Firecrawl needs a minute after first start (RabbitMQ and the queue database initialise). Tuning and
upgrades: `deploy/crawl/README.md`.

Rollback: `ssh sldesktoplocal 'cd /opt/jobboard && docker compose -p crawl down'`; set `CRAWL=0` in the
bootstrap for later runs. The app runs without it (FIRECRAWL_URL/SEARXNG_URL just fail to connect).

## 5. Hub bootstrap with the DNAT disabled

```
scp deploy/hub-bootstrap.sh uscentral1:/home/sam/jobboard-bootstrap/hub-bootstrap.sh
ssh -t uscentral1 'sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh --check'      # read the plan once more
ssh -t uscentral1 'sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh'              # apply: table, forward accepts, masquerade, Cloudflare sets + timer, ip_forward
```

Public 443 still terminates on the hub (the DNAT rule is absent without the flag). Verify nothing broke and
the Cloudflare sets are filled:

```
ssh uscentral1 'sudo nft list table inet jobboard_hub | head -40; sudo nft list set inet jobboard_hub cloudflare_v4 | grep -c /; systemctl list-timers jobboard-cloudflare-ips.timer --no-pager'
ssh sldesktop true; curl -sI https://jobs.slindskog.net/login.html | head -1     # both still the old paths
```

Rollback: `ssh -t uscentral1 'sudo nft delete table inet jobboard_hub'` removes the live table; the inserted
accepts are tagged `comment "jobboard-hub"` (delete by handle: `sudo nft -a list ruleset | grep jobboard-hub`);
the boot config backup is printed by the script (`/root/jobboard-hub-backup/<ts>/nftables.conf`, copy back).
Then `sudo systemctl disable --now jobboard-cloudflare-ips.timer jobboard-hub-accepts.service`.

## 6. Cutover

Order matters: freeze, copy the data, point the public path at the desktop, then let the merge deploy there.

```
# 6a. freeze: no pushes to master from here on; stop scans on the hub (MCP `jobboard`: cancel_run if one is running)

# 6b. database dump + secrets on the hub (the bootstrap's maintenance modes work there unchanged)
ssh -t uscentral1 'touch /home/sam/jobboard-bootstrap/dump /home/sam/jobboard-bootstrap/export-secrets && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh'   # one mode per run: dump, then export-secrets
mkdir -p /tmp/jobboard-cutover && chmod 700 /tmp/jobboard-cutover
scp uscentral1:/home/sam/jobboard-bootstrap/jobscan-dump.sql.gz uscentral1:/home/sam/jobboard-bootstrap/jobboard.env /tmp/jobboard-cutover/
scp /tmp/jobboard-cutover/jobscan-dump.sql.gz /tmp/jobboard-cutover/jobboard.env sldesktoplocal:/home/sam/jobboard-bootstrap/
ssh uscentral1 'rm -f /home/sam/jobboard-bootstrap/jobboard.env'                   # the dump can stay as the first off-box backup
cp /tmp/jobboard-cutover/jobscan-dump.sql.gz ~/Desktop/jobscan-backup-$(date +%F).sql.gz   # keep one copy on the laptop

# 6c. adopt the secrets and restore the database on the desktop (two runs: adoption is a full run, restore is a mode)
ssh -t sldesktoplocal 'sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh'            # adopts jobboard.env (rotates the db password), redeploys, skips the dump (table not empty)
ssh -t sldesktoplocal 'touch /home/sam/jobboard-bootstrap/restore && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh'   # drop, recreate, restore, health check
```

If the restore fails it says so, leaves the app container stopped (a half-restored database must not
serve) and keeps the dump: fix the cause and re-run the restore mode, or start the app by hand with the
command it prints.

Now the desktop holds the hub's data and tokens: Claude's existing token (`~/.config/jobboard/token`)
works against it. Check through the second MCP registration or directly:

```
curl -s -H "Authorization: Bearer $(cat ~/.config/jobboard/token)" http://10.88.0.2:8090/mcp -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 200
```

```
# 6d. public path -> desktop
ssh -t uscentral1 'sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh enable-dnat'
curl -sI https://jobs.slindskog.net/login.html | head -1                             # 200 from the desktop now
ssh sldesktoplocal 'tail -3 /var/log/nginx/jobs.slindskog.net.access.log'           # the request landed here, client IP = your IP (CF-Connecting-IP)
curl -sI https://slindskog.net/ | head -1                                           # homepage too
```

Log in at https://jobs.slindskog.net in a browser (same password: the hash was adopted) and open a job.

```
# 6e. MCP registration: replace the old one
claude mcp remove jobboard; claude mcp remove jobboard-desktop
claude mcp add --transport http jobboard http://10.88.0.2:8090/mcp --header "Authorization: Bearer $(cat ~/.config/jobboard/token)"

# 6f. GitHub Actions: deploy through the hub's 2222 forward to the desktop
ssh-keyscan -p 2222 72.14.178.40 2>/dev/null                                       # the DESKTOP's host keys as seen through the forward
gh secret set DEPLOY_PORT --body 2222
ssh-keyscan -p 2222 72.14.178.40 2>/dev/null | gh secret set DEPLOY_KNOWN_HOSTS
gh secret list                                                                     # DEPLOY_HOST stays 72.14.178.40; DEPLOY_SSH_KEY unchanged (same deploy key, installed on the desktop in step 3)
ssh -i ~/.ssh/jobboard_deploy -p 2222 deploy@72.14.178.40 jobboard-deploy origin/next   # the forced command works through the forward

# 6g. merge next -> master: the workflow tests and deploys origin/master to the desktop
git checkout master && git merge --ff-only next && git push origin master
ssh sldesktoplocal 'rm -f /home/sam/jobboard-bootstrap/deploy-ref'                 # right away: a later bootstrap re-run must track master, not next
gh run watch                                                                       # then: curl -sI https://jobs.slindskog.net/login.html

# 6h. unfreeze; stop the hub's stack (data volume kept until step 7)
ssh -t uscentral1 'cd /opt/jobboard && sudo docker compose -f jobboard/compose.prod.yaml --env-file .env stop'
```

Update the memory notes / README afterwards (MCP is at http://10.88.0.2:8090/mcp; the bootstrap is
staged on `sldesktoplocal`; backups via the `dump` mode).

Rollback (any point in 6, as long as the hub's stack still exists): `ssh -t uscentral1 'sudo bash
/home/sam/jobboard-bootstrap/hub-bootstrap.sh disable-dnat'` puts 443 back on the hub within a second;
`sudo docker compose ... start` there if it was stopped; `claude mcp add ... http://10.88.0.1:8090/mcp`
for MCP; `gh secret delete DEPLOY_PORT` and the old known_hosts for Actions. Data written on the desktop
after the restore would have to be dumped there (`dump` mode) and restored on the hub (`restore` mode,
the same bootstrap) if the rollback comes late.

## 7. Retire the old installation on the hub (after a few days)

```
ssh -t uscentral1 'sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh --check retire-app'    # prints the plan
ssh -t uscentral1 'touch /home/sam/jobboard-bootstrap/retire-app && sudo bash /home/sam/jobboard-bootstrap/hub-bootstrap.sh retire-app'
```

Removes the stack (volume `jobboard_pgdata` kept: `docker volume rm jobboard_pgdata` when you are sure),
the old `table inet jobboard` (the hub's previous input-drop policy; read the "input filtering now" lines it
prints), the nginx site, `/opt/jobboard`, the `deploy` user and its sudoers rule. `/etc/sudoers.d/jobboard-bootstrap`
stays (remove with the old bootstrap's `remove-sudo-rule` mode once the hub needs nothing more).

Rollback: no automatic one; the backups under `/root/jobboard-hub-backup/<ts>/` hold the nftables config, and
the pgdata volume holds the data as of 6h. Re-running the old bootstrap from `master` before this change
would rebuild the installation.

## Afterwards: routine operations on the desktop

```
# backup (also the off-box copy):
ssh -t sldesktoplocal 'touch /home/sam/jobboard-bootstrap/dump && sudo bash /home/sam/jobboard-bootstrap/bootstrap.sh' && scp sldesktoplocal:/home/sam/jobboard-bootstrap/jobscan-dump.sql.gz ~/Desktop/jobscan-backup-$(date +%F).sql.gz
# nginx / deploy-script / crawl changes: stage and re-run the bootstrap (idempotent); app changes: push to master
# firewall / tunnel state: show-firewall mode; watchdog: journalctl -u jobboard-wg-watch -n 20
```
