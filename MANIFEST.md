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
  user accounts, scan + tag-to-owner after ingest, and `oidc on|off` (sign in through Authelia,
  matched by username, local login kept). Also a CLI for the menu.
- `notify.py` — webhook + e-mail notifications (requester on done/denied, admin on pending)
- `library.py` — tag-scoped, path-confined reads of the Calibre library for downloads, the
  book page and covers; opens metadata.db `mode=ro` and falls back to `immutable=1` (loudly)
  only after an unclean stop
- `metadata.py` — the metadata provider chain (bookinfo.pro → Hardcover mirror → Open
  Library), circuit breaker, negative cache; background only; drops provider genres/tags at
  the adapter boundary
- `dedupe.py` — the 'in library' match: ISBN / Calibre UUID, then title + author, one scoped
  read per search page
- `bookmeta.py` — metadata-first search (Open Library): works, work and author records, series
  from the Goodreads mirror, and `copies()` — a book's catalogue cross-links resolved into
  downloadable copies, plus the keyword ladder, each verified by `matching.py`
- `matching.py` — Readarr-style weighted distance (identifier 10, language 5, format 5, author 3,
  title 3; auto at <= 0.20), edition flags (abridged/adapted, omnibus), language codes
- `wanted.py` — keep looking: matching and the widening recheck schedule
- `catalogs.py` — the admin's own OPDS catalogs (any number), each a first-class source
- `shelfmark_api.py` — Shelfmark's pending requests on the portal's Pending card (service login)
- `fetch_wheels.py` — build-time fallback when pypi.org's index stalls: fetches the pinned files
  through its plain HTML pages; pip then installs them hash-checked with no index
- `share.py` — family sharing: a book the family already has is given to the next reader (their
  owner tag added to the same copy) instead of downloaded again; strong matches only
- `comics.py` — comics and manga (docs/COMICS.md): requests, the family copy first, the search and
  the queueing THROUGH Shelfmark as the reader, arrivals (CBR/CB7 repacked as CBZ with unar,
  matched to the request, ComicInfo.xml + ComicBookInfo written in), which comics need a Kobo copy
  (an owner whose Kobo syncs, or 'Make Kobo copy'), Kindle jobs that need a Kindle copy first
- `follows.py` — v5.8: follow a comic/manga series, a book series or an author; the daily check
  (first check records what is out, then what came out since becomes a notice); one tap (a comic
  request, a book request through bookreq.py, or Pick in Shelfmark); mail digests; the admin's
  daily count
- `bookreq.py` — v5.8.3: one-tap book requests: the family copy first, approvals, Shelfmark's
  Prowlarr search scored by bookrel.py, the copy CONFIRMED by the reader (BOOK_CONFIRM), queued in
  Shelfmark as the reader; the arrived file CHECKED before the import (title, author, language,
  ISBN against Hardcover's editions, format) and held when it is not the book; 'Wrong book';
  a failed or missing download tries the next release
- `bookrel.py` — which release is the book asked for: the title as a phrase with nothing left over,
  the author's surname, no packs/abridged/summaries, EPUB first and no PDF, the reader's language
- `templates/book_list.html` — a book series' or an author's books (Hardcover, cached 6 h) with
  what the reader and the family have, Get it / Add to mine / Pick
- `home.py` — v6.0: a reader's home page: reading (Calibre-Web state), listening (Audiobookshelf
  progress), next in each series they read, recently added; each part on its own
- `audiorel.py` — v6.0: which release is the audiobook asked for (the book rules, audio formats,
  M4B and unabridged preferred, narrators taken out of the name before the title check)
- `hcwant.py` — v6.0: a reader's Hardcover Want to Read list feeds Get it requests (opt-in;
  the first sync only records the list)
- `dash.py` — v6.0: the admin dashboard's "What needs you" and the week in numbers
- `static/app.js` — v6.0: the portal's only script (autosubmit filters, filter-as-you-type, copy
  buttons, self-refreshing Requests); every page works without it
- `templates/hub.html`, `hub_base.html`, `help/*.html` — v6.0: home.<domain>, the start page,
  setup checklist and the 13 guides; `home.html` — the portal's home page
- `sendcode.py` — v5.9.1: send a book to an e-reader's own browser with a 4-character code
  (/send, no login; the book only reaches the browser holding the code's secret cookie)
- `hcaudio.py` — v5.9.1: Audiobookshelf listening progress to each reader's own Hardcover
  (their Devices token), matched by ASIN / ISBN / exact title+author, never reopening a Read book
- `mangadex.py` — v5.9: which chapters a manga volume holds (MangaDex aggregate), only for the
  MangaDex entry linked to the same MangaUpdates id; pre-ticks the chapters a volume replaces
- `metrontrack.py` — v5.9: a reader's own Metron account (Devices); finished Western comics found
  through Metron are scrobbled to their Metron collection, once each
- `hardcover.py` — book series and authors from Hardcover (needs HARDCOVER_API_KEY):
  search(Series|Author), a series' books, an author's books; no compilations or duplicates;
  `python -m hardcover` checks the key and the queries
- `anilist.py` — v5.8: a reader's AniList (authorization code; the token in the portal database
  only), exact-title matching of a series, the highest FINISHED volume from Calibre-Web's read
  state sent as progressVolumes, never lowered
- `templates/following.html`, `_notices.html` — Following, and the New for you list (also on the
  home page)
- `comicmeta.py` — what a series is: Metron / ComicVine (Western, account / key) and MangaUpdates
  (manga, manhwa, manhua; no key), cached a day; kind and reading direction; `python -m comicmeta check`
- `comicrel.py` — which release is the one asked for: parses a release title (series, volume,
  issue, chapter range, year, language, digital, edition, pack) and applies the hard rules and the
  scoring of docs/COMICS.md ("Classification")
- `templates/comics.html`, `comic_series.html` — the Comics page and a series' issues/volumes;
  `audiobooks.html` — My audiobooks, each a download (one file, or one ZIP for a folder)
- `filemeta.py` — title/author out of MOBI/AZW3 (EXTH) and FB2, read-only and bounded, so the
  host job finds those books in Calibre after the import (also once CWA converted them)
- `templates/remove.html` — 'Remove from my library': what happens, and how to clear the
  copies on a Kobo and a Kindle
- `kindle.py` — SMTP Send-to-Kindle (+ `python -m kindle test addr`)
- `fetchers.py` — provider registry + adapters (Gutenberg, Standard Ebooks, IA, LibriVox)
- `opds.py` — OPDS catalog search-and-grab (your own catalog)
- `enrich.py`, `imap.py` (plain or TLS IMAP, sender authentication), `tagger.py`,
  `auth.py`, `db.py` (requests, prefs, lockout counters, audit trail), `config.py`
- `requirements.txt`, `Dockerfile` (1 worker process, `BIND` env), `.dockerignore`
- `templates/` — base, login, index (direct catalogue search), **books** (metadata search),
  **work** (a book and its verified copies), **writer** (any author), status (incl. Still looking,
  Sent to Kindle, Shelfmark approvals), upload, **library**, **devices**, **admin** (incl. your
  catalogs), **book**, **author**, **series**
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
- `alert.sh` — one entry point for alerts: portal (webhook/e-mail), else journal + direct ntfy/webhook post.
  `ALERT_SEQ` gives a problem and its all-clear one ntfy Sequence-ID (the all-clear replaces it on
  the phone); `ALERT_TAGS` / `ALERT_CLICK` set the emoji and what tapping it opens
- `disk-report.sh` — daily at `DISK_REPORT_HOUR` (09:05, cron `bookstack-diskreport`): the admin's
  disk summary on the alert channel: how full, growth since yesterday and over the week, days
  until `DISK_WARN_PCT`, where the space went (ebooks, audiobooks, seedbox copies, Docker),
  memory. One Sequence-ID, so today's replaces yesterday's. `DISK_REPORT=false` turns it off
- `kuma-push.sh` — `kuma-push.sh <job> up|down [msg]`: a scheduled job reports to its Kuma push
  monitor (selftest, disk, metapush, cfips, backup, canary); a no-op until monitoring is set up
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
- `metadata-push.sh` — every 2 min (cron `bookstack-metapush`, under flock, output to the journal), three
  passes, all through CWA's own tools as PUID:PGID with the owner tag checked before and after:
  (1) fill-only metadata (title/sort/authors/series/description/publisher/date/language/ISBN
  and a missing cover, fetched only from provider image hosts); (2) L10 owner tags for books
  whose file cannot carry one — refused if the book already has an owner; (3) on-demand
  format conversions with Calibre's `ebook-convert`, added to the same book
- `prune.sh` — backup retention (forget + prune) with whichever key it is given: nightly from
  `backup.sh` when that key may delete, monthly with `/etc/bookstack/restic-prune.env`, or from
  the admin's own computer (`RESTIC_PRUNE_ENV=./prune.env bash prune.sh`) (L15)
- `heal.sh` — every 2 min: restart a container Docker reports unhealthy twice, at most once per
  30 min, alerting every time (L20)
- `cert-watch.sh` — daily: Caddy's certificates, the origin-pull trust, this zone's own
  origin-pull certificate and the Cloudflare token, alerting ahead of expiry (L09, L14)
- `update-check.sh` — weekly: newer releases for every pinned image, one alert per version (L06)
- `synthetic.py` — the canary journey, twice a day (`bookstack-canary.timer`, L08)
- `caddy-clientip.sh` — has Caddy loaded Cloudflare's address list? (recent Cloudflare-delivered
  requests vs their resolved client address); Deploy/Update restart Caddy until it has, heal.sh
  restarts it (at most hourly) when it has not
- `comic-convert.sh` — every 3 min when comics are on (cron `bookstack-comics`, flock): KCC in a
  throwaway container (no network, PUID:PGID, `KCC_MEMORY` cap), one at a time. Adds the colour,
  fixed-layout Kobo copy to the same Calibre book as KEPUB (tags read before and after); makes the
  Kindle copy of a comic being sent (KCC's Send-to-Kindle EPUB, lower quality, then split into
  parts to stay under the mail limit) in `library/staging/kindle-comics/<job>/`
- `mem-tidy.sh` — nightly 03:45 (cron `bookstack-memtidy`): restarts an idle Calibre-Web,
  Shelfmark, Audiobookshelf or Syncthing whose memory grew past 70 % of its limit
- `seedbox-fetch.py` — every 20 seconds: hands finished seedbox downloads (SABnzbd jobs, torrents
  rTorrent reports complete) from the Syncthing copy (`library/seedbox-sync`) to `library/seedbox`
  for Shelfmark; checks every folder here is Receive Only (pauses one that is not), allowlisted
  Syncthing requests, never changes the seedbox. `--setup` / `--check` for Library → Seedbox
- `gate-sync.py` — portal password changes into Authelia's user file (path unit + timer, L05)

## Files bookstack.sh generates on the HOST (outside $STACK_DIR, not in this repo)
- `/etc/bookstack/restic.env` (0600 root) — repository, password, S3 keys; `RESTIC_APPEND_ONLY=1`
  when the nightly key cannot delete (then `restic-prune.env`, if the prune key lives here, and
  `bookstack-prune.{service,timer}` on the 15th)
- `/etc/bookstack/aop/` (0700 root) — this zone's origin-pull CA and client certificate + keys (L14)
- `/etc/bookstack/canary.env` (0600 root) + `bookstack-canary.{service,timer}` — the canary
  accounts' passwords and their twice-daily journey (L08)
- `bookstack-gate-sync.{service,path,timer}` — only while the Authelia gate is on (L05)
- `/etc/bookstack/seedbox.env` (0600) + `seedbox.state` + `bookstack-seedbox.{service,timer}` —
  Library → Seedbox: the seedbox's Syncthing device ID, rTorrent address and login, what was
  already handed over
- `$STACK_DIR/authelia/oidc-jwks.pem` (0600, uid 1000) — Authelia's OpenID Connect signing key,
  made when the gate is enabled; Audiobookshelf signs in through it (L05)
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
- `kuma-upgrade-test.sh` — v5.9.1: Uptime Kuma 1.23.17 -> 2.5.5-slim on the same data, the way
  Operations -> Update moves it (every monitor, the channel, the window and the push tokens survive)
- `monitoring-test.sh` — `kuma_bootstrap.py` against the real pinned Uptime Kuma, a webhook
  receiver and GreenMail (setup, idempotence, drift repair, push, /metrics, both alert channels)
- `caddy-build-test.sh` — builds `caddy/Dockerfile`, checks `caddy version` is the release it asks
  for (v2.10.2 while GHSA-6365-7ppr-5r92 is open) and its three plugins, and validates the full
  production Caddyfile (every optional site, the Authelia gate injected) on it

## docs/
- `COMICS.md` — comics and manga: the path of a request (through Shelfmark), classification and
  scoring, device copies, what the owner sets up
- `DEPLOYMENT-CHECKLIST.md` — what is verified locally per version and what to check on the VPS
- `RESEARCH-GAPS.md` — the v4.2 research sweep: VPS sizing, 20 pre-deploy items (done), 22 later
- `DECISIONS-PENDING.md` — blockers and deliberate deferrals
- `BOOKORBIT.md` — BookOrbit study, trial results and the optional overlay design
