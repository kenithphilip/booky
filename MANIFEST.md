# bookstack — file manifest

Copy this whole folder to the VPS and run `bash bookstack.sh` (see README.md).

## Top level
- `bookstack.sh` — the menu: Install & deploy (Quick install), Users & devices, Library,
  Security, Operations. Re-runnable. `BOOKSTACK_LIB=1` sources it without running (tests).
- `docker-compose.yml` — all services (hardened), including Shelfmark (CWA companion)
- `docker-compose.authelia.yml` — optional SSO overlay (Security → Authelia)
- `docker-compose.ephemera.yml` — optional Ephemera + FlareSolverr overlay (Operations; see its
  header: upstream project is gone, built from a pinned re-upload, Tailscale-only)
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
- `library.py` — tag-scoped, path-confined reads of the Calibre library for downloads
- `kindle.py` — SMTP Send-to-Kindle (+ `python -m kindle test addr`)
- `fetchers.py` — provider registry + adapters (Gutenberg, Standard Ebooks, IA, LibriVox)
- `opds.py` — OPDS catalog search-and-grab (your own catalog)
- `enrich.py`, `dedupe.py`, `imap.py` (plain or TLS IMAP, sender authentication), `tagger.py`,
  `auth.py`, `db.py` (requests, prefs, lockout counters, audit trail), `config.py`
- `requirements.txt`, `Dockerfile` (1 worker process, `BIND` env), `.dockerignore`
- `templates/` — base, login, index, status, upload, **library**, **devices**, **admin**
- `tests/` — pytest suite (`test_app`, `test_core`, `test_auth_cwa`, `test_abs`,
  `test_review_fixes`) + `fixtures/` (schemas dumped from the real CWA / Calibre DBs)

## caddy/ — reverse proxy
- `Dockerfile` — Caddy built with Cloudflare DNS + IP list + rate limiter
- `Caddyfile.template` — hardened config (mTLS origin lock via trust_pool, headers, rate
  limits incl. `/api/auth/*`, real client IP from CF-Connecting-IP, tailnet-only admin sites,
  torrents/Ephemera blocks rendered only when enabled, Authelia gate markers)

## authelia/ — optional self-hosted SSO + 2FA
- `configuration.yml.template` (notifier block swapped to SMTP when mail is set), `users_database.yml`,
  `caddy-gate.snippet`, `inject-gate.py` (per-host anchored bypass lists)

## scripts/
- `cf-ips.sh` — firewall allowlist synced to Cloudflare ranges (nightly)
- `backup.sh` — encrypted restic backup (retention from `RESTIC_KEEP_*`, default 7/4/6; probes
  for `--retry-lock`, which Debian 12's restic 0.14 does not have)
- `restore-test.sh` — proves the backup restores (config + DB snapshots) and reports the full-restore size
- `disk-watch.sh` — hourly: alert at `DISK_WARN_PCT`, stop the downloaders and raise
  `library/staging/.disk-paused` (the portal's own imports honour it) at `DISK_STOP_PCT`,
  start them again below `DISK_RESUME_PCT`
- `alert.sh` — one entry point for alerts: portal (webhook/e-mail), else journal + direct ntfy/webhook post
- `selftest.sh` — non-destructive health/security check (Operations → Self-test)

## configs/fail2ban/
- `jail.local` (template) — SSH jail (local firewall) + three Caddy jails that ban at
  Cloudflare via `cloudflare-token`. Both Caddy login filters are host-scoped templates
  (`@@DOMAIN@@`, rendered by `render_fail2ban`): `caddy-auth.conf` counts failed POST logins
  on request./shelf./auth. by the real client IP, `caddy-abs-login.conf` covers audio. under
  its own looser threshold, and `caddy-device-auth.conf` covers Basic auth on /opds + /kosync

## tests/ (not shipped to the server)
- `run-unit.sh` — runs the portal tests inside the shipping image
- `tui-test.sh` — installer logic harness (stubbed prompts/docker)
- `stack-test.sh` + `docker-compose.test.yml` + `e2e_driver.py` — end-to-end UAT on the real
  containers

## docs/
- `DEPLOYMENT-CHECKLIST.md` — what is verified locally per version and what to check on the VPS
- `RESEARCH-GAPS.md` — the v4.2 research sweep: VPS sizing, 20 pre-deploy items (done), 22 later
- `DECISIONS-PENDING.md` — blockers and deliberate deferrals
- `BOOKORBIT.md` — BookOrbit study, trial results and the optional overlay design
