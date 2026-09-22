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
  dropbox / torrent watchers, retries/backoff
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
- `enrich.py`, `dedupe.py`, `imap.py` (plain or TLS IMAP), `qbittorrent.py`, `tagger.py`,
  `auth.py`, `db.py` (requests, prefs, lockout counters, audit trail), `config.py`
- `requirements.txt`, `Dockerfile` (1 worker process, `BIND` env), `.dockerignore`
- `templates/` — base, login, index, status, upload, **library**, **devices**, **admin**
- `tests/` — pytest suite (`test_app`, `test_core`, `test_auth_cwa`, `test_abs`,
  `test_review_fixes`) + `fixtures/` (schemas dumped from the real CWA / Calibre DBs)

## caddy/ — reverse proxy
- `Dockerfile` — Caddy built with Cloudflare DNS + IP list + rate limiter
- `Caddyfile.template` — hardened config (mTLS origin lock via trust_pool, headers, rate
  limits incl. `/api/auth/*`, tailnet binds, Authelia gate markers)

## authelia/ — optional self-hosted SSO + 2FA
- `configuration.yml.template`, `users_database.yml`, `caddy-gate.snippet`

## scripts/
- `cf-ips.sh` — firewall allowlist synced to Cloudflare ranges (nightly)
- `backup.sh` — encrypted restic backup (7 daily / 4 weekly / 6 monthly)
- `restore-test.sh` — proves the backup restores (config + DB snapshots, beside the stack)
- `disk-watch.sh` — hourly: alert at 85 %, stop downloaders at 95 %, restart below 80 %
- `alert.sh` — one entry point for e-mail/webhook alerts from cron and the scripts
- `selftest.sh` — non-destructive health/security check (Operations → Self-test)

## configs/fail2ban/
- `jail.local` (template) — SSH jail (local firewall) + Caddy login jail that bans at
  Cloudflare via `cloudflare-token`; `caddy-auth.conf` — matches failed POST logins by the
  real client IP only

## tests/ (not shipped to the server)
- `run-unit.sh` — runs the portal tests inside the shipping image
- `tui-test.sh` — installer logic harness (stubbed prompts/docker)
- `stack-test.sh` + `docker-compose.test.yml` + `e2e_driver.py` — end-to-end UAT on the real
  containers

## docs/
- `DEPLOYMENT-CHECKLIST.md` — what is verified locally per version and what to check on the VPS
- `RESEARCH-GAPS.md` — the v4.2 research sweep: VPS sizing, 20 pre-deploy items (done), 22 later
- `DECISIONS-PENDING.md` — blockers and deliberate deferrals
