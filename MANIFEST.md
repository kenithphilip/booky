# bookstack — file manifest

Copy this whole folder to the VPS and run `bash bookstack.sh` (see README.md).

## Top level
- `bookstack.sh` — the menu: Install & deploy (Quick install), Users & devices, Library,
  Security, Operations. Re-runnable. `BOOKSTACK_LIB=1` sources it without running (tests).
- `docker-compose.yml` — all services (hardened), including Shelfmark (CWA companion), the shared
  FlareSolverr (profile `solver`) and the one-shot `kuma-bootstrap` (profile `tools`)
- `docker-compose.authelia.yml` — optional SSO overlay (Security → Authelia)
- `docker-compose.ephemera.yml` — optional Ephemera overlay (Operations; see its header: upstream
  project is gone, built from a pinned re-upload, Tailscale-only). Uses the shared FlareSolverr
- `.env.example` — documents every setting (bookstack.sh writes the real `.env`, quoted)
- `.gitignore` — keeps rendered configs, secrets and runtime data out of version control
- `README.md`, `MANIFEST.md`

## librarian/ — the portal (Flask)
- `app.py` — routes: login, search, request, approve/deny, retry/dismiss, upload, **My books**
  (download, Send-to-Kindle), **Devices** (Kindle address, Kobo link, preferences), **admin**
  dashboard (+ add user), intake webhook, health. CSRF on every form.
- `worker.py` — background engine: queue, atomic ingest + owner-tagging, auto-Kindle,
  dropbox watcher, ABS tag-job retries, housekeeping, retries/backoff
- `cwa.py` — the only writer to CWA's app.db: users (with isolation tag), passwords, Kindle
  address, Kobo tokens, Kobo-sync / registration toggles (+ WAL checkpoint so Shelfmark sees
  changes). Also the CLI the menu calls.
- `abs.py` — Audiobookshelf automation: bootstrap (root, API key, library), tag-restricted
  user accounts, scan + tag-to-owner after ingest. Also a CLI for the menu.
- `notify.py` — webhook + e-mail notifications (requester on done/denied, admin on pending)
- `library.py` — tag-scoped, path-confined reads of the Calibre library for downloads, the
  book page and covers; opens metadata.db `mode=ro` and falls back to `immutable=1` (loudly)
  only after an unclean stop
- `metadata.py` — the metadata provider chain (bookinfo.pro → Hardcover mirror → Open
  Library), circuit breaker, negative cache; background only; drops provider genres/tags at
  the adapter boundary
- `dedupe.py` — the 'in library' match: ISBN / Calibre UUID, then title + author, one scoped
  read per search page
- `kindle.py` — SMTP Send-to-Kindle (+ `python -m kindle test addr`)
- `fetchers.py` — provider registry + adapters (Gutenberg, Standard Ebooks, IA, LibriVox)
- `opds.py` — OPDS catalog search-and-grab (your own catalog)
- `enrich.py`, `imap.py` (plain or TLS IMAP, sender authentication), `tagger.py`,
  `auth.py`, `db.py` (requests, prefs, lockout counters, audit trail), `config.py`
- `requirements.txt`, `Dockerfile` (1 worker process, `BIND` env), `.dockerignore`
- `templates/` — base, login, index, status, upload, **library**, **devices**, **admin**,
  **book**, **author**, **series**
- `tests/` — the portal's pytest suite, every `test_*.py` in that directory (a hand-kept list
  here went stale twice; `bash tests/run-unit.sh` runs whatever is present) + `fixtures/`
  (schemas dumped from the real CWA / Calibre DBs)

## monitoring/ — Uptime Kuma configuration (build context of `kuma-bootstrap`)
- `kuma_bootstrap.py` — idempotent: admin account, the alert channels alert.sh uses, one monitor
  per service / enabled feature, push monitors for the scheduled jobs, the nightly-reboot
  maintenance window, 30-day history. Owns only monitors it marked; config on stdin
- `Dockerfile`, `requirements.txt`, `requirements.lock` (hash-pinned `uptime-kuma-api`)

## caddy/ — reverse proxy
- `Dockerfile` — Caddy built with Cloudflare DNS + IP list + rate limiter
- `Caddyfile.template` — hardened config (mTLS origin lock via trust_pool, headers, rate
  limits incl. `/api/auth/*`, real client IP from CF-Connecting-IP, tailnet-only admin sites,
  torrents/Ephemera blocks rendered only when enabled, Authelia gate markers, and a 403 on
  CWA's unauthenticated admin jobs and on `/api/v3/*` + `/api/UserStorage/*`, which CWA
  relays verbatim to readingservices.kobo.com for anyone who asks)

## authelia/ — optional self-hosted SSO + 2FA
- `configuration.yml.template` (notifier block swapped to SMTP when mail is set), `users_database.yml`,
  `caddy-gate.snippet`, `inject-gate.py` (per-host anchored bypass lists)

## scripts/
- `cf-ips.sh` — firewall allowlist synced to Cloudflare ranges (nightly)
- `backup.sh` — encrypted restic backup (retention from `RESTIC_KEEP_*`, default 7/4/6; probes
  for `--retry-lock`, which Debian 12's restic 0.14 does not have). Weekly `restic check`
  re-reads one 52nd of the packs (`--read-data-subset=n/52`), the counter rotating in
  `/etc/bookstack/backup.state`, so every byte is verified exactly once a year
- `restore-test.sh` — proves the backup restores (config + DB snapshots) and reports the full-restore size
- `disk-watch.sh` — hourly: alert at `DISK_WARN_PCT`, stop the downloaders and raise
  `library/staging/.disk-paused` (the portal's own imports honour it) at `DISK_STOP_PCT`,
  start them again below `DISK_RESUME_PCT`. The percentage is the worse of blocks-used and
  inodes-used, and the alert names which tripped (the remedies are unrelated)
- `alert.sh` — one entry point for alerts: portal (webhook/e-mail), else journal + direct ntfy/webhook post
- `kuma-push.sh` — `kuma-push.sh <job> up|down [msg]`: a scheduled job reports to its Kuma push
  monitor (selftest, disk, metapush, cfips, backup); a no-op until monitoring is set up
- `selftest.sh` — non-destructive health/security check (Operations → Self-test). Includes the
  library's own isolation invariant (books with no `owner:<user>` tag, and owner tags naming
  accounts that no longer exist — both are invisible to everybody), orphan dropbox folders, and
  a Shelfmark login probe that catches an app.db which vanished AFTER start. Pings
  `HEALTH_PING_URL` (`<url>` / `<url>/fail`) so an unattended run is also a dead-man's switch.

## configs/fail2ban/
- `jail.local` (template) — SSH jail (local firewall) + three Caddy jails that ban at
  Cloudflare via `cloudflare-token`. Both Caddy login filters are host-scoped templates
  (`@@DOMAIN@@`, rendered by `render_fail2ban`): `caddy-auth.conf` counts failed POST logins
  on request./shelf./auth. by the real client IP, `caddy-abs-login.conf` covers audio. under
  its own looser threshold, and `caddy-device-auth.conf` covers Basic auth on /opds + /kosync.
  That last one counts TWO statuses: 401 (the /opds and /kosync/users/auth challenge) and 400,
  which is how CWA answers a wrong password on /kosync/syncs/progress — the endpoints the
  401-only filter never saw, so the jail could not fire on the one real password oracle

## scripts/ additions
- `metadata-push.sh` — every 15 min (cron `bookstack-metapush`, output to the journal): the
  portal's queued metadata into Calibre via CWA's own `calibredb`, as PUID:PGID, allowlisted
  to title/sort/authors/series/series_index, owner tag checked before and after each write

## Files bookstack.sh generates on the HOST (outside $STACK_DIR, not in this repo)
- `/etc/bookstack/restic.env` (0600 root) — repository, password, S3 keys
- `/etc/bookstack/disk.state` — the disk watchdog's latch and last-alert stamp
- `/etc/bookstack/postboot.sh` (0755 root, regenerated on every Deploy / Update / Backups) and
  `/etc/systemd/system/bookstack-postboot.service` — a oneshot that runs `scripts/selftest.sh`
  after every boot, so the 04:30 unattended reboot is verified while nobody is watching. A
  failure alerts through `scripts/alert.sh` and shows on the TUI's first screen. Full result:
  `$STACK_DIR/.postboot-selftest.log`, or `journalctl -u bookstack-postboot`.
- `/etc/bookstack/selftest-hourly.sh` + `/etc/systemd/system/bookstack-selftest.{service,timer}` —
  the hourly self-test (scheduled mode), reported to Kuma, falling back to alert.sh;
  `/etc/bookstack/selftest.state` holds its last pass/fail. Result: `$STACK_DIR/.selftest-hourly.log`
- `/etc/systemd/system/bookstack-{backup,restore-test}.{service,timer}` and
  `bookstack-alert@.service` (written by every installer that points OnFailure= at it);
  `/etc/cron.d/bookstack-{disk,cfips,metapush}`

## tests/ (not shipped to the server)
- `run-unit.sh` — runs the portal tests inside the shipping image
- `tui-test.sh` — installer logic harness (stubbed prompts/docker)
- `stack-test.sh` + `docker-compose.test.yml` + `e2e_driver.py` — end-to-end UAT on the real
  containers
- `monitoring-test.sh` — `kuma_bootstrap.py` against the real pinned Uptime Kuma, a webhook
  receiver and GreenMail (setup, idempotence, drift repair, push, /metrics, both alert channels)

## docs/
- `DEPLOYMENT-CHECKLIST.md` — what is verified locally per version and what to check on the VPS
- `RESEARCH-GAPS.md` — the v4.2 research sweep: VPS sizing, 20 pre-deploy items (done), 22 later
- `DECISIONS-PENDING.md` — blockers and deliberate deferrals
- `BOOKORBIT.md` — BookOrbit study, trial results and the optional overlay design
