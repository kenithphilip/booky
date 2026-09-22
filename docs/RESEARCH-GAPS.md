# bookstack — research gaps and recommendations

Date: 2026-09-22. Scope: the repo at this commit (v4.1: Audiobookshelf automation, portal
lockout/audit/quota/CSP, e-mail notifications, self-service password, KOReader toggle,
fail2ban keyed on `client_ip` with the `cloudflare-token` action, Caddy rate limits on
`/download` and `/intake`, safe zip extraction, plain IMAP, inline Ephemera Dockerfile).
Seven research/audit lenses produced ~70 candidate findings; this document is the merged,
de-duplicated result after checking each claim against the current files. Items the repo
already does are listed once under "Already in place" and not repeated.

Each item carries a stable id (`N..` = do before the first deploy, `L..` = later). The same
ids are used in the implementation plan handed to the engineer.

---

## 1. Executive summary

The stack is in better shape than most of the candidate findings assume: the v4.1 changes
already closed the fail2ban self-ban, the missing lockout/audit trail, the unautomated
Audiobookshelf path, and most of the test-coverage gaps. What remains falls into five groups:

1. **Two real correctness bugs in the installer that will bite on the first run.**
   `askpw2` draws a whiptail message box *inside* a command substitution, so one mistyped
   password stores terminal escape bytes as the admin, user or restic password (N02). Deploy
   starts Caddy — and therefore publishes `books.`/`audio.` to the internet — before the
   admin password is set and before Audiobookshelf has a root user; whoever visits
   `audio.<domain>` first can claim it (N01).
2. **Trust-boundary holes in the portal**: a logged-in user controls the URL the worker
   fetches from the host network namespace (SSRF, credential leak of the OPDS catalog
   login), and the e-mail/webhook intake never checks that the target user exists or that the
   sender is allowed (N03). Caddy's admin API is reachable by every host-network process
   and holds the Cloudflare zone token in cleartext (N04).
3. **Device promises that break silently**: the Authelia gate has no bypass for the
   Audiobookshelf apps, `/kosync` or `/intake` (N05); non-EPUB uploads import untagged and
   are invisible to the uploader (N06); Send-to-Kindle will happily mail AZW3/MOBI which
   Amazon rejects, and the "kepub" download choice never exists (N07); the recommended
   Cloudflare Bot Fight Mode cannot be exempted for Kobo/OPDS on the Free plan and the
   default 200 MB upload cap is above Cloudflare's 100 MB body limit (N10).
4. **Operations on a 4 GB / 80 GB box**: no memory or PID limits (N11), backups copy live
   SQLite files and the restore test proves nothing (N12), nothing watches the disk (N13),
   `Update` pulls `:latest` for six upstreams with no backup or health gate (N14), two pinned
   Python packages have known CVEs (N15).
5. **Quality**: the owner-tagger re-zips every EPUB uncompressed, bloating files 3–300× and
   pushing some over the Kindle mail limit (N08); the worker never recovers `importing` rows
   after a restart and re-creates an error row every 10 s for a file that keeps failing (N09);
   non-Latin filenames are rejected on upload (N19).

Twenty items are recommended before the first deploy; all are small or medium. The rest
(single sign-on into the apps, CWA's native auto-send, pinned image digests with update
notices, an alert channel, a canary user journey, taking the portal off the host network)
are scheduled for later and do not block go-live.

**VPS**: buy the X8 (4 cores / 8 GB / 160 GB, EUR 9.99). The X4 runs the core stack, but
its 80 GB disk caps the audiobook library at roughly 40–50 GB and leaves no headroom for
Shelfmark's browser-backed sources, Ephemera, or a large first Audiobookshelf scan. Details
in section 2.

---

## 2. VPS recommendation

Plans (EUR/month): X2 1c/2 GB/40 GB = 3.29 · X4 2c/4 GB/80 GB = 5.79 · X8 4c/8 GB/160 GB = 9.99 · X16 6c/16 GB/240 GB = 15.99.

### Memory

Measured peaks from the repo's own end-to-end run (README "VPS sizing", five runs on arm64)
plus published figures for the services the run does not exercise:

| Component | Typical resident | Peak seen / documented | Fence proposed (N11) |
|---|---|---|---|
| Debian + systemd + sshd + tailscaled + fail2ban + dockerd/containerd | 350–500 MB | — | — |
| calibre-web (CWA, bundles full Calibre) | 350–450 MB | 450–575 MB with one import + Kindle fixer; 200–600 MB more per `ebook-convert`, 1–2 GB on large scanned PDFs | 1600 MB |
| audiobookshelf | 100–150 MB | 150–200 MB; first scan of a big library can reach 0.5–1.5 GB (Node heap) | 768 MB + `NODE_OPTIONS=--max-old-space-size=640` |
| shelfmark (standard image with Chromium) | 140–200 MB | 2 GB is upstream's "safe minimum" if Direct-Download sources launch the browser | 768 MB, browser sources off on X4 |
| qbittorrent (libtorrent 2, mmap cache) | 100–150 MB | unbounded page-cache growth while seeding unless "Disable OS cache" is set | 512 MB |
| librarian (portal, 1 process × 8 threads) | ~70 MB | ~150 MB | 384 MB |
| caddy | ~55 MB | ~150 MB | 256 MB |
| uptime-kuma | ~150 MB | 300 MB (slow growth over weeks reported) | 384 MB |
| authelia (optional) | ~30 MB | ~240 MB measured during argon2 hashing | 384 MB |
| aria2 + AriaNg | ~40 MB | ~120 MB | 128 + 64 MB |
| ephemera + flaresolverr (optional) | 450 MB | 1.5 GB+ (Chromium per solve; on-box pnpm/tsc/vite build needs 1–2 GB) | 1024 MB, X8 only |
| restic backup/prune (host, nightly) | 0 | 200–500 MB | — |

Core stack idle ≈ 1.8–2.3 GB including the OS. Worst realistic coincidence on X4 (one
conversion + an ABS scan + a Shelfmark browser solve) ≈ 4.5 GB, i.e. into the 2 GB swap the
installer creates. Not fatal, but slow, and today nothing stops the kernel from OOM-killing
CWA or ABS mid-import because no service has a memory limit (docker-compose.yml x-common
sets only `no-new-privileges` and log rotation).

### Disk (the real constraint)

X4: 80 GB − OS/Docker ~5 GB − swap 2–4 GB − images 8–12 GB (CWA ~3 GB, Shelfmark ~1.5 GB,
xcaddy builder, transient doubling during `Update`) − ebooks 10–20 GB − ABS metadata 1–3 GB
− restic cache 1–3 GB − in-flight torrents/staging ≈ **50–55 GB for audiobooks**, i.e.
100–150 titles at 300–500 MB each. README already says "audiobooks fill 60 GB fastest".
X8: ~130 GB free ≈ 300+ titles. X16 only if the library really heads past ~150 GB.

### CPU

2 shared vCPUs are fine for a small group: `ebook-convert` is mostly single-threaded
(30–120 s per book), ABS scans are ffprobe-bound, Chromium solves peg a core for 10–60 s. The
pain on X4 is contention when a scan overlaps a conversion; 4 cores remove it.

### Bandwidth

Irrelevant at this scale: 20 users streaming 64–128 kbps audio plus Kobo/Kindle syncs plus
incremental restic is a few hundred GB/month. 10 TB vs 15 TB does not matter.

### Recommendation

- **X8 (EUR 9.99)**. The extra EUR 4.20/month (EUR 50/year) buys 2× disk (the binding
  constraint for audiobooks), 2× RAM (no swap during conversion + scan + browser), and 2×
  cores. It is the plan on which the stack is "excellent to manage" without tuning.
- **X4 is defensible** if all of these hold: audiobooks stay under ~40 GB on local disk,
  Ephemera stays off, Shelfmark Direct-Download (browser) sources stay off, ≤ ~10 users, and
  N11 (memory fences, 4 GB swap, `SHELFMARK_CONCURRENCY=1`) is applied. Watch `df` at 70 %.
- **X2 is not viable** (CWA + Docker alone would live in swap). **X16** only for disk.
- Do not offload audiobooks to an rclone/object-storage mount to stay on X4: ABS keys items
  by inode and re-probes everything when they change, qBittorrent must not write to FUSE,
  and Shelfmark's audiobook destination would need a VFS write cache on local disk anyway.
  If the provider sells attachable block volumes, that is the only offload that behaves
  like local disk (mount it at `library/audiobooks`; the compose bind mounts are relative).
- Upgrade signals (X4 → X8): swap used > 500 MB for hours, `dmesg | grep -i oom` or
  `OOMKilled=true` on any container, conversions taking > 3–5 min, ABS scans > 30 min,
  `df /srv` > 70 %, nightly backup not finishing before morning.

---

## 3. Already in place since the sweep (candidates dropped)

Checked against the files; no action needed beyond the residuals named.

- **fail2ban Caddy jail**: `configs/fail2ban/caddy-auth.conf` keys on `"client_ip"`, counts
  only `POST /login|/api/auth/login|/api/firstfactor` → 401, and `jail.local` uses
  `backend = auto` for the jail and the `cloudflare-token` action (ban at the edge). The
  portal returns 401 on a failed login (`app.py:114`). The three "fail2ban is broken"
  candidates are obsolete. Residual: `selftest.sh` should assert the jail is active (N18).
- **Portal login policy**: per user+IP and per IP lockout (`db.py:62-99`), 12 h permanent
  sessions, audit table, daily quota, CSP with `script-src 'none'`, `X-Frame-Options DENY`.
  Residual: sessions are never re-validated or revoked (N17).
- **KOReader sync**: `Library → Formats` toggles `koreader_sync_enabled` and the Devices
  page shows the `/kosync` server. Residual: the Authelia bypass lists do not include
  `/kosync` (N05).
- **Audiobookshelf**: `abs.py` bootstraps root + API key + library, creates tag-restricted
  users, and the worker tags each new audiobook to its owner. Residual: Shelfmark's
  audiobook destination bypasses this (N20); two TUI texts still say "tag it by hand"
  (`bookstack.sh:565,570,789`, README:199-200).
- **Notifications**: requester e-mail on done/denied/error, admin e-mail on pending, webhook.
  Residual: no operational alert channel for backups/disk/containers (N12/N13 minimal, L07).
- **Self-service password change** across portal/CWA/Shelfmark/ABS. Residual: Authelia's
  own hash is not updated (L05).
- **Rate limits** on `/download/*` and `/intake`; **safe zip extraction** with member and
  8 GiB unpacked caps; **plain IMAP**; **Ephemera inline Dockerfile**.
- **Tests** already cover the simulated Kobo `/v1/library/sync`, OPDS Basic auth, intake
  webhook, dropbox drops, Send-to-Kindle/auto-Kindle via a real SMTP sink, IMAP intake,
  the production Authelia snippet, ABS bootstrap/users/tagging, lockout/quota/audit.
- Confirmed **not** findings: origin lock design (mTLS + Cloudflare-only firewall), CSRF,
  path-confined downloads, tag-scoped reads, loopback binds, `.env` quoting, Shelfmark start
  ordering, unattended-upgrades, key-only SSH baseline.

---

## 4. Features

### N06 — Non-EPUB uploads are invisible to the uploader; tag PDF/CBZ before import and stop converting them
- **What**: `worker.ingest_local_file` (`worker.py:140-142`) places PDF/MOBI/AZW3/CBZ/TXT/FB2
  in `/ingest` raw and reports "done: set tag owner:<user> in CWA". Under Allowed Tags the
  book never appears for that user in CWA, OPDS, Kobo sync or My books — only the admin sees
  it. `upload.html` promises the opposite; the isolation guide (`bookstack.sh:565`) claims the
  tag is added before conversion, which is not what the code does. Two users importing the
  same title within CWA's pickup window also overwrite each other (`worker.py:63-64`,
  `os.rename` onto an identical `Author - Title.epub`). CWA additionally converts every PDF
  and comic to a reflowed EPUB (`apply_library_defaults` sets `auto_convert=1` and never
  sets `auto_convert_ignored_formats`), which is both slow on 2 cores and poor quality.
- **Why**: this is the common non-EPUB journey (a PDF, a comic, a Shelfmark AZW3) and it
  dead-ends; it also contradicts "end users only touch the portal".
- **How**: (1) `tagger.add_owner_tag_pdf(path, tag)` with `pypdf`: merge `owner:<user>` into
  the Info `/Keywords` (comma-separated, keep existing) — Calibre's PDF reader maps
  Keywords to tags. (2) `tagger.add_owner_tag_cbz(path, tag)`: set the zip comment to a
  `ComicBookInfo/1.0` JSON block with `"tags":["owner:<user>"]` — Calibre's archive reader
  parses it. (3) Route both from `ingest_local_file` before `_place_raw_ingest`; reject CBR at
  upload ("convert to CBZ"). (4) For mobi/azw3/fb2/txt set the request to a new terminal
  state `needs-tag` (not `done`) and list those on `/admin` with the book id once imported;
  post-import reconciliation is L10. (5) `apply_library_defaults`: add
  `auto_convert_ignored_formats='pdf,cbz,cbr,cb7'`; expose it in `step_formats`. (6) Unique
  ingest names: `f"{base} [{owner}-{rid or uuid4().hex[:6]}].{ext}"` (CWA reads metadata from
  the file, not the name). (7) Fix `upload.html` and the isolation guide to say exactly which
  formats are auto-tagged. (8) e2e: upload a 1-page PDF and a 2-image CBZ as alice; assert
  the owner tag in `metadata.db`, visibility in alice's `/library` and `/opds/new`, absence
  for bob, and that the CBZ was not converted.
- **Effort**: medium. **Priority**: high.

### N20 — Shelfmark audiobooks bypass owner tagging
- **What**: `docker-compose.yml:214-217` sends Shelfmark audiobooks straight to
  `/audiobooks` as `Author/Title/`, so the worker's ABS tagging never runs; README:199 says
  "tag owner:<user> in ABS". Every other audiobook path is now automatic.
- **How**: `DESTINATION_AUDIOBOOK=/dropbox/{User}` and `FILE_ORGANIZATION_AUDIOBOOK=rename`
  so the file (m4b/zip) lands flat in the user's dropbox and `scan_dropbox_once` handles it
  via `_place_audio_file` (already tags in ABS). Extend the dropbox watcher to also accept a
  *settled directory* (no file modified for `SETTLE_SECONDS`) as an audiobook folder and move
  it whole into `AUDIO_DIR/<owner> - <dirname>` for the cases where a source delivers a
  folder. Update README:199-200, `step_shelfmark` (line 789), `step_isolation` (line 570).
  e2e: drop a directory with two mp3s into `dropbox/alice`, assert ABS tag.
- **Effort**: small. **Priority**: high.

### L04 — Use CWA's native per-user auto-send instead of mailing the raw EPUB pre-import
- **What**: `_auto_kindle` runs on the `.part` file before CWA imports it (`worker.py:62`), so
  the mailed copy never gets the Kindle EPUB fixer or fetched metadata, and non-EPUB uploads
  are never auto-sent. CWA v4 has per-user `auto_send_enabled` with a delay
  (`auto_send_delay_minutes`), post-conversion, using CWA's own SMTP — which the Mail menu
  currently tells the admin to configure by hand in the GUI (`bookstack.sh:549`).
- **How**: `cwa.set_mail(...)` writing `settings.mail_server/mail_port/mail_use_ssl/
  mail_login/mail_from/mail_server_type=0` and `mail_password_e` (Fernet with the key CWA
  keeps at `/config/.key` — verify against `cps/config_sql.py` for the running version);
  `cwa.set_auto_send(name, on)`; Devices' auto-send checkbox drives `user.auto_send_enabled`
  when CWA mail is configured, else falls back to the portal SMTP; stack-test with the
  GreenMail sink.
- **Effort**: medium. **Priority**: medium (later).

### L05 — One login when Authelia is on: header SSO into CWA/portal/Shelfmark + password sync + e-mail reset
- **What**: README accepts double login and a separate Authelia password store;
  `authelia_add_user` keeps its own argon2 hash, and the self-service password change does
  not update it.
- **How**: when the gate is enabled set CWA `config_allow_reverse_proxy_header_login=1`,
  `config_reverse_proxy_login_header_name='Remote-User'`; add `request_header -Remote-User`
  (and `-Remote-Groups/-Email/-Name`) *before* `forward_auth` in the gate snippet on **all**
  paths so a client can never inject it; portal `login_required` accepts `Remote-User` for an
  existing CWA user when `AUTHELIA_ENABLED`; Shelfmark `AUTH_METHOD=proxy`; Authelia
  `notifier.smtp` from `SMTP_*` for self-service reset; portal password change re-hashes the
  Authelia entry (argon2 via `argon2-cffi` in the librarian image). Keep `/kobo`, `/opds`,
  `/kosync` on their own Basic/token auth.
- **Effort**: medium. **Priority**: medium (later).

### L10 — Post-import owner reconciliation for formats that cannot carry a tag
- **What**: after N06, mobi/azw3/fb2/txt/djvu still import untagged.
- **How**: on placement record `(rid, owner, basename, placed_at)`; a 30 s loop opens
  `metadata.db` read-only, finds `data.name` matching the placed basename with `timestamp ≥
  placed_at` and no owner tag, then adds the tag in one short `BEGIN IMMEDIATE` transaction
  (`tags`, `books_tags_link`, `books.last_modified`) — requires `./library/books` mounted rw
  for librarian. Mark the request `done`; after CWA's `ingest_timeout_minutes` mark `error`.
  This is the only place the portal would write Calibre's DB; keep it tiny.
- **Effort**: medium. **Priority**: medium (later).

### L11 — Real KEPUB downloads with kepubify (or drop the option)
- **What**: `config.FORMATS` offers `kepub` and Devices labels it "(Kobo)", but CWA converts
  to EPUB and only kepubifies transiently on Kobo sync, so `library.best_format` silently
  falls back to EPUB. N07 relabels it now; this makes it real.
- **How**: pinned, checksum-verified `kepubify` binary in `librarian/Dockerfile`;
  `library.file_for(owner, id, 'kepub')` converts the EPUB once into a size-capped cache
  under `/state` and serves `<title>.kepub.epub`.
- **Effort**: medium. **Priority**: low (later).

### L12 — Kobo card: prerequisites, pitfalls, last-sync indicator; Magic Shelves / shelves-only sync / Hardcover token
- **How**: Devices Kobo card gets a "before you start" list (store/Libby stop working while
  linked because `config_kobo_proxy=0`; sign-in/factory reset rewrites `api_endpoint`; only
  EPUB/KEPUB sync; first sync takes minutes) and "Last sync: <time>, <n> books" from
  `kobo_synced_books`/`kobo_reading_state` (read-only); a "Test my link" action; optional
  setters for `user.kobo_only_shelves_sync` and `user.hardcover_token`
  (`config_kobo_sync_magic_shelves=1`, `config_hardcover_sync=1` in defaults). All columns
  exist in `librarian/tests/fixtures/cwa_app_schema.sql`.
- **Effort**: small. **Priority**: low (later).

### L13 — CWA auto-metadata fetch on ingest (with tag rewriting disabled)
- **How**: `auto_metadata_fetch_enabled=1, auto_metadata_smart_application=1,
  auto_metadata_update_tags=0, auto_metadata_enforcement=1` in `apply_library_defaults`
  plus a `Library → Metadata` provider menu. `auto_metadata_update_tags` must stay 0 (it
  would replace `owner:<user>`); N18 guards it.
- **Effort**: small. **Priority**: medium (later).

### L16 — Shelfmark request policies and a unified approval queue
- **What**: the portal enforces `APPROVALS_REQUIRED`; Shelfmark downloads straight into the
  dropbox with no approval. Upstream ships per-source request policies (download directly /
  must request / blocked) and an admin approve/decline flow with a static API key.
- **How**: add "Settings → Request policies: set every enabled source to *must request* for
  non-admins" as step 0 of `step_shelfmark` and to `docs/DEPLOYMENT-CHECKLIST.md`; later,
  pull `GET /api/requests` into the portal's Pending card. Verify the feature exists in the
  Shelfmark version pinned by N14 before wiring the API.
- **Effort**: small (docs) / medium (API). **Priority**: medium (later).

### L18 — Large uploads over the tailnet
- **What**: N10 caps browser uploads at 95 MB because of Cloudflare; audiobook zips are bigger.
- **How**: `upload.<domain> { bind @@TAILSCALE_IP@@; import private_tls; import hardening;
  request_body { max_size 2GB }; reverse_proxy 127.0.0.1:8090 }`, grey-cloud DNS record in
  `step_cloudflare`, and `app.upload` raising the limit when `X-Forwarded-Host` starts with
  `upload.`. Until then the per-user dropbox over Tailscale/Syncthing is the documented path.
- **Effort**: small. **Priority**: low (later).

---

## 5. Security

### N01 — Deploy publishes books./audio. before credentials exist
- **What**: `step_deploy` starts `caddy` in the first `compose up` (`bookstack.sh:274`) and
  only asks for the admin password afterwards (`:287-291`); if the prompt is cancelled the
  factory `admin/admin123` stays live with only a notice. Audiobookshelf's root user is
  created by whoever hits `audio.<domain>` first; `step_abs_setup` runs later (Quick install
  offers it after Deploy, `:373`). Certificates land in CT logs within minutes and the
  hostnames are scanned quickly; Cloudflare proxying does not hide them.
- **How**: (1) drop `caddy` from the first `compose up`; after `app.db` exists and the
  portal is healthy, loop the admin-password prompt until `ADMIN_PW_SET=true` (or generate
  and display a random one — no silent fall-through); (2) if `absctl status` reports
  `isInit=false`, run `step_abs_setup` inline (root password prompt) before Caddy; (3) only
  then `compose up -d caddy`; (4) `selftest.sh`: FAIL if `curl 127.0.0.1:13378/status` has
  `isInit:false`, FAIL if `POST http://127.0.0.1:8083/login` with `admin/admin123` is
  accepted (redirect instead of the login page); (5) `tui-test.sh`: assert `up -d caddy`
  is recorded after `passwd admin`.
- **Effort**: small. **Priority**: high.

### N03 — Inbound trust: client-supplied download URLs (SSRF + credential leak), unvalidated intake/IMAP targets
- **What**: `make_request` (`app.py:143-148`) accepts `download_url`, `source`, `is_torrent`
  from the form and checks only that `source` is a *known* key (disabled sources pass). The
  worker GETs it from the librarian container, which runs `network_mode: host`
  (`docker-compose.yml:116`): 127.0.0.1:2019 (Caddy admin, see N04), :8080 (qBittorrent),
  :6800 (aria2 RPC), :13378 (ABS), :9091, :3001, the Tailscale local API and cloud metadata
  are all reachable. `_auth_for` (`worker.py:25-28`) attaches `MYCATALOG_USER/PASS` to *any*
  URL whose `source` says `mycatalog`, so a user can exfiltrate the admin's OPDS credentials
  to their own host. With `is_torrent=1` the URL goes to qBittorrent verbatim. The approval
  card (`status.html:5-7`) shows title/author/source but never the URL, so approvals do not
  help; admins and `/intake` skip approval. `/intake` (`app.py:424`) and `imap._target_user`
  (`imap.py:10-16`) never check that the user exists (`cwa._valid_name` + `cwa.get_user`), so
  any sender who knows the mailbox address can plant files in any user's library (and, with
  auto-Kindle on, on their Kindle). `imap._SAFE` strips `/` so real path traversal is
  neutralised; `+..` resolves to the container root and fails on permissions, but junk owner
  directories/tags are still created.
- **How**: (1) `make_request`: require `config.SOURCES[source] is True`; re-validate
  `download_url` against a per-adapter host allowlist added to `fetchers.py`
  (`gutenberg`: `www.gutenberg.org` or the `GUTENBERG_MIRROR` host; `standard_ebooks`:
  `standardebooks.org`; `internet_archive`/`librivox`: `archive.org`, `*.archive.org`;
  `mycatalog`: `urlsplit(MYCATALOG_URL).netloc`, https only) and `https` scheme; limit
  `is_torrent` to `internet_archive` when `IA_USE_TORRENT`. Better still, cache search
  results server-side per session and POST an opaque id. (2) `worker._download` and
  `Qbit.add`: resolve the host and refuse loopback, RFC1918, link-local, ULA and CGNAT
  (100.64/10 — Tailscale) addresses; `allow_redirects=False` loop re-checking each hop;
  `MAX_DOWNLOAD_MB` (default 500) enforced on `Content-Length` and the byte counter.
  (3) `_auth_for`: only when `urlsplit(url).netloc == urlsplit(MYCATALOG_URL).netloc`.
  (4) `status.html`: show `urlsplit(download_url).hostname` on the Pending card. (5)
  `/intake` and `imap`: 400 / drop unless `cwa._valid_name(user) and cwa.get_user(user)`;
  new `IMAP_ALLOWED_SENDERS` (comma list) or require `From` to match the CWA user's e-mail;
  cap attachment size at `MAX_UPLOAD_MB`; `scan_dropbox_once` skips directories that are
  not valid existing users. (6) Tests: `test_app.py` — request with
  `download_url=http://127.0.0.1:2019/config/` → 400; `source=mycatalog` with a foreign host
  never gets credentials; `imap` with `+nosuchuser` is dropped; e2e negative case.
- **Effort**: medium. **Priority**: high.

### N04 — Caddy: admin API on the host loopback with the Cloudflare token in cleartext; Kobo tokens in access logs; qBittorrent Host header
- **What**: Caddy runs on the host network and the global block (`Caddyfile.template:2-12`)
  never sets `admin`, so the unauthenticated admin API listens on `127.0.0.1:2019` of the
  host. `{$CF_API_TOKEN}` (`:40`, `:53`) is expanded at adapt time, so
  `GET http://127.0.0.1:2019/config/` returns the zone-wide DNS token and the basic_auth
  hash to any local process (the portal on the host network, or anything reached via N03).
  Separately, the hardening snippet logs every URI (`:27-33`), so each Kobo sync writes the
  per-user bearer-equivalent token (`/kobo/<32hex>`) to `caddy/data/access.log`, readable by
  uid 1000 (= every container). And `dl.<domain>` proxies qBittorrent without rewriting the
  Host header (`:94-102`); qBittorrent's Host-header validation (on by default) answers
  "Unauthorized", so the admin download UI does not work out of the box.
- **How**: (1) global `admin unix//run/caddy-admin.sock`; `reload_caddy` becomes
  `caddy reload --config /etc/caddy/Caddyfile --address unix//run/caddy-admin.sock`
  (the existing `|| compose restart caddy` fallback stays); (2) replace both `{$CF_API_TOKEN}`
  with `{env.CF_API_TOKEN}` (runtime placeholder, never in the adapted config); (3) log
  `format filter { wrap json  fields { request>uri regexp /kobo/[0-9a-fA-F]{32}
  /kobo/REDACTED  request>headers>Authorization delete  request>headers>Cookie delete } }`
  — the fail2ban regex only needs `client_ip`, `method`, `uri`, `status`, which stay
  intact; (4) `dl.` vhost: `reverse_proxy 127.0.0.1:8080 { header_up Host
  {upstream_hostport} }`; (5) `selftest.sh`: FAIL if `ss -ltn` shows `:2019`; (6) tui-test:
  rendered Caddyfile still validates (the harness already runs `caddy validate`).
- **Effort**: small. **Priority**: high.

### N15 — Vulnerable pinned Python packages, unpinned transitives
- **What**: `librarian/requirements.txt` pins `gunicorn==22.0.0` (CVE-2024-6827, TE.CL
  request smuggling, fixed in 23.0.0) and `requests==2.32.3` (CVE-2024-47081, `.netrc`
  credential leak, fixed in 2.32.4). `Flask==3.0.3` is old but *not* affected by
  CVE-2025-47278 (that bug is specific to 3.1.0). Werkzeug, itsdangerous, urllib3, lxml's
  libxml2 float to whatever pip resolves at build time; the base image is the moving
  `python:3.12-slim`. The librarian is behind Caddy (which normalises framing), so the
  smuggling CVE is mitigated in practice — fix it anyway, it is a one-line change.
- **How**: `gunicorn>=23.0.0`, `requests>=2.32.4`, `Flask>=3.1.1`, current `lxml`, plus
  `pypdf` (N06); generate `requirements.lock` with `pip-compile --generate-hashes` and
  install with `--require-hashes`; add `uvx pip-audit -r librarian/requirements.lock` to
  `tests/run-unit.sh`; pin `python:3.12.<x>-slim` (or `@sha256`) in the Dockerfile.
- **Effort**: small. **Priority**: high.

### N16 — Secrets pass through argv; `.env` ends up owned by uid 1000; Authelia YAML is built by heredoc
- **What**: `caddy hash-password --plaintext "$pw"` (`bookstack.sh:196`), `lib passwd/add-user
  --password "$pw"` (`:289,396,436`), `authelia crypto hash generate argon2 --password "$4"`
  (`:707`) all expose passwords in `ps`/`docker inspect` for their lifetime; the `cwa`/`abs`
  CLIs (`cwa.py:234-239`) accept only `--password`. `step_configure` does `chmod 600` then
  `chown -R 1000:1000 "$STACK_DIR"` (`:208`), so `.env` (CF token, SMTP/IMAP, Authelia
  secrets, intake token) is readable by the uid every container runs as. `authelia_add_user`
  interpolates display name/e-mail unescaped into YAML (`:720-727`); a quote breaks
  `users_database.yml` and Authelia fails closed (502 for everyone).
- **How**: `--password-stdin` on both CLIs (`sys.stdin.read().rstrip("\n")`) and
  `printf '%s' "$pw" | lib passwd "$u" --password-stdin` (the `lib` helper already uses
  `docker exec -i`); `printf '%s' "$pw" | docker run --rm -i caddy:2 caddy hash-password`
  (reads stdin when `--plaintext` is omitted); Authelia hash via `docker run --rm -e
  PW="$4" ... sh -c 'authelia crypto hash generate argon2 --password "$PW"'` so the host argv
  never carries it; after the recursive chown add `chown root:root "$ENV_FILE"` (compose
  runs as root) and make `selftest.sh` check owner as well as mode; validate the Authelia
  username with the same `[A-Za-z0-9._-]+` rule and write the entry with `python3 -c
  'import yaml; yaml.safe_dump(...)'` or JSON-quoted scalars. Update `tui-test.sh` stubs.
- **Effort**: small. **Priority**: medium.

### N17 — Portal sessions are never revoked; backslash open redirect; GET logout
- **What**: `session["admin"]` is fixed at login and `login_required` never re-reads the
  user (`app.py:62-79`), so a password reset, removal or demotion leaves an existing cookie
  valid for the full 12 h. `_safe_next` (`app.py:82`) allows `/\evil.tld`, which browsers
  treat as `//evil.tld`. `/logout` accepts GET (`:118`).
- **How**: `auth.verify` returns a fingerprint `sha256(password_hash)[:16]`; store it and
  `session["chk"]=time()` at login; a `before_request` for authenticated routes re-reads
  `cwa.get_user` at most every 60 s, clears the session if the user is gone or the
  fingerprint changed, and refreshes `session["admin"]` from the role bits. `_safe_next`:
  `p=urlsplit(nxt)`; allow only `not p.scheme and not p.netloc and nxt.startswith('/') and
  '\\' not in nxt`. `/logout` POST-only if `base.html` already posts (it does per the
  candidate; confirm). Add a "Rotate portal secret (logs everyone out)" entry under Security.
  Tests: removed user's session is bounced; the three redirect payloads stay on-site.
- **Effort**: small. **Priority**: medium.

### L01 — Container isolation: portal off the host network, per-service networks, `cap_drop`, `read_only`, Shelfmark sees only `app.db`
- **What**: librarian is on the host network only to reach qBittorrent/aria2 on 127.0.0.1;
  everything else shares one bridge, so Shelfmark (third-party, internet-facing) can talk to
  qbittorrent:8080, authelia:9091, etc.; no `cap_drop`/`read_only`; caddy/kuma run as root;
  Shelfmark mounts the whole `cwa/config` (all password hashes, Kobo tokens, CWA SMTP
  password) although it needs `app.db` only; the `books` host user is in the `docker` group
  (root-equivalent) with a login shell although nothing runs docker as that user.
- **How**: `ports: ["127.0.0.1:8090:8090"]` for librarian on named networks (`front` with
  Caddy-facing services, `dl` with qbittorrent/aria2), `QBIT_URL=http://qbittorrent:8080`,
  `ABS_URL=http://audiobookshelf:80`, real qB credentials from `.env` (config.py already
  supports `QBIT_USER/PASS`); `x-common`: `cap_drop: [ALL]` (LSIO images need
  `CHOWN,SETUID,SETGID,DAC_OVERRIDE,FOWNER` back), `read_only: true` + tmpfs where feasible;
  Shelfmark mounts `./cwa/config/app.db:/auth/app.db:ro` only (it opens it `immutable=1`);
  `/etc/docker/daemon.json` with `icc:false`, `live-restore:true`; `useradd --shell
  /usr/sbin/nologin` and drop `usermod -aG docker`. Depends on N11's limits.
- **Effort**: large. **Priority**: medium (later).

### L14 — The origin lock trusts Cloudflare's *shared* origin-pull CA
- **What**: the CA in `cf-origin-pull-ca.pem` signs the same client cert for every Cloudflare
  tenant, so mTLS proves "some Cloudflare edge", not "my zone". Caddy's per-hostname certs
  blunt the classic bypass (the attacker needs an Enterprise Host/SNI override), but the
  README's "rejected at the TLS handshake" wording overstates the guarantee.
- **How**: preferred — Cloudflare Tunnel (`cloudflared` on 127.0.0.1) so the VPS has no public
  web ports; cheaper — a per-zone custom AOP certificate uploaded via the API and trusted
  instead of the shared PEM. Reword README/DEPLOYMENT-CHECKLIST either way.
- **Effort**: medium. **Priority**: medium (later).

### L15 — Backups deletable from the host they protect
- **What**: `backup.sh:13` runs `restic forget --prune` nightly with the same key as the
  backup, so root on the VPS (or the leaked B2 key) can wipe every snapshot.
- **How**: an append-only B2 application key (no `deleteFiles`) or `rest-server
  --append-only` for the nightly job; prune monthly from a separate `/etc/bookstack/
  restic-prune.env` or from the laptop. Pair with N12.
- **Effort**: small. **Priority**: medium (later).

### L17 — Bot friction on the portal login and a default nudge towards 2FA
- **How**: Cloudflare Turnstile (free) on `login.html` with server-side siteverify; Quick
  install asks "Enable SSO + 2FA now? (recommended)"; `selftest.sh` warns when
  `forward_auth` is absent.
- **Effort**: small. **Priority**: low (later).

### L19 — Host hardening extras
- **How**: sshd drop-in adds `AllowUsers root books`, `LoginGraceTime 20`,
  `ClientAliveInterval 300`, `AllowTcpForwarding no`, `AllowAgentForwarding no`,
  `PermitEmptyPasswords no`, `sshd -t` before reload; sysctl adds `kernel.kptr_restrict=2`,
  `kernel.dmesg_restrict=1`, `kernel.yama.ptrace_scope=1`, `kernel.unprivileged_bpf_disabled=1`,
  `fs.protected_{symlinks,hardlinks,fifos,regular}`; install Tailscale from its signed apt
  repo instead of `curl | sh` (`bookstack.sh:161`); `cf-ips.sh` drops port 80 (certs are
  DNS-01 and Cloudflare forces HTTPS) and validates each line with a CIDR regex before
  touching ufw; persistent journald. (Tailscale key expiry is handled now in N18.)
- **Effort**: small. **Priority**: low (later).

---

## 6. Operations

### N02 — bookstack.sh: `askpw2` corrupts passwords; errexit is off inside `step || true`; Cancel clears settings
- **What**: whiptail draws its UI on stdout and returns results on stderr, which is why
  `ask`/`askpw` swap fds. `msg()` (`bookstack.sh:21`) does not, and `askpw2` calls `msg` for
  "At least 8 characters" / "They did not match" while every caller runs it as
  `pw=$(askpw2 ...)` (`:180,288,318,394,435,693,735`). Inside the substitution the message box
  is invisible to the user (frozen screen until Enter) and its escape sequences are prepended
  to the returned password. One mistype at Deploy therefore stores `<ESC…>password` for
  `admin`; at Backups it becomes the restic repository password nobody can retype.
  `tui-test.sh:34` stubs `askpw2` entirely, so the harness cannot see it. Separately, Bash
  disables `set -e` for the whole body of a function invoked in a `||` list, so
  `step_system || true` continues past a failed Docker install to "System ready", and
  `step_deploy` continues past a failed `compose build`. And `ask` returns non-zero on
  Cancel but several callers ignore it: Cancel at "Kindle e-mail (blank clears it)"
  (`:421`) clears the address; Cancel at IMAP username (`:760`) or SMTP username (`:543`)
  blanks the setting.
- **How**: `msg(){ whiptail --title Bookstack --msgbox "$1" 18 78 >/dev/tty </dev/tty; }`
  and the same for `big`; defensively reject any password containing `$'\e'` where it is
  consumed. Add explicit `|| { msg ...; return 1; }` to the critical commands (`apt-get
  install docker-ce…`, `compose build`, `compose up`, `curl` of the origin-pull CA). Audit
  every `x=$(ask …)` for Cancel: `|| return 1` where a value is required, and require a
  typed `none` where "blank clears" is intended. `tui-test.sh`: stop stubbing `askpw2`;
  stub whiptail's `--msgbox` to print marker bytes to stdout and feed `short`,
  `goodpass-123`, `goodpass-123`; assert the captured value is exactly `goodpass-123`; add
  "Cancel at the Kindle prompt leaves the address untouched" and "compose build failure makes
  step_deploy return 1".
- **Effort**: small. **Priority**: high.

### N09 — Worker: no restart recovery, error-row flood, swallowed exceptions, `/healthz` always "ok"
- **What**: `db.claim_one` flips a row to `importing` (`db.py:143-159`); `run_forever`
  (`worker.py:263-275`) never requeues such rows after a restart and the admin UI can only
  retry `error`. `scan_dropbox_once` (`:147-179`) inserts a row *before* ingest and leaves the
  file in place on exception, so a file that fails deterministically (permissions, disk
  full) produces a fresh `error` row every 10 s. Both watchers and the IMAP poller
  `except Exception: pass` (`:181-187, :259-260`), so a permission error on a dropbox is a
  silent no-op. A crash between `copyfile` and `rename` leaves `<uuid>.part` in `/ingest`
  forever. `/healthz` returns 200 regardless of whether the queue thread is alive, so
  compose, Kuma and selftest all stay green while nothing is processed.
- **How**: `db.recover_on_start()`: `importing` with a re-fetchable URL → `queued`
  ("requeued after restart"); `importing` with `download_url='local'` → `error`
  ("interrupted by restart"); `downloading` older than 24 h → `error`. Call it first in
  `run_forever`. In `scan_dropbox_once` look up an existing row for `(owner, name, source=
  'dropbox')` before `db.add`; on failure move the file to `<dropbox>/<user>/.failed/` (hidden
  → skipped) and record once. Replace the bare `pass` with `logging.getLogger("worker")
  .exception(...)`. On startup sweep `/ingest/*.part` and `*/.*.uploading` older than 1 h into
  `/staging/quarantine/`. Module-level `HEARTBEAT = {}` set by each loop; `/healthz` returns
  JSON and 503 when any heartbeat is older than 120 s, `INGEST_DIR` is not writable, free
  space < 1 GiB, or `app.db` cannot be opened; include `ingest_pending` and `ingest_oldest_s`.
  The compose healthcheck already points at `/healthz`. Tests: `test_recover_on_start`,
  `test_dropbox_failure_records_one_row_and_parks_file`, healthz 503 with a stale heartbeat;
  e2e step 14 asserts all heartbeats < 60 s.
- **Effort**: small. **Priority**: high.

### N11 — Resource fences for a 4 GB box
- **What**: no `mem_limit`, `mem_reservation` or `pids_limit` anywhere; the OOM killer picks
  the largest RSS (CWA mid-conversion or ABS mid-scan) and the container restarts with a
  half-processed file. Swap is 2 GB regardless of RAM; no `vm.swappiness`; inotify limits are
  default (ABS crashes when watch limits are hit on large libraries); `SHELFMARK_CONCURRENCY`
  defaults to 2.
- **How**: per-service limits from the table in section 2 (X4 values; ×1.5 on X8),
  `pids_limit: 512` in `x-common` (1024 for calibre-web), `oom_score_adj: -500` on caddy and
  librarian, `NODE_OPTIONS=--max-old-space-size=640` on audiobookshelf, `flaresolverr
  1024m` in the Ephemera overlay, `authelia 384m` in its overlay. `step_system`: swapfile
  4 GB when RAM ≤ 4 GB; append `vm.swappiness=10`, `fs.inotify.max_user_watches=524288`,
  `fs.inotify.max_user_instances=512` to `90-bookstack.conf`. `envdefault
  SHELFMARK_CONCURRENCY 1`. `selftest.sh`: FAIL if any container has `OOMKilled=true`
  (`docker inspect -f '{{.State.OOMKilled}}'`), warn if `MemAvailable` < 512 MB or Ephemera
  is enabled on < 8 GB. Note in the README sizing table which fence to raise on X8.
- **Effort**: small. **Priority**: high.

### N12 — Backups: consistent SQLite snapshots, real restore test, failure alert
- **What**: `backup.sh:9-13` restics `$STACK_DIR` while every container writes: `cwa/config/
  app.db` and `cwa.db` (WAL), `library/books/metadata.db`, `librarian/state/librarian.db`,
  `abs/config/absdatabase.sqlite`, `kuma/data/kuma.db`, `authelia/db.sqlite3`. A committed
  transaction may live only in the `-wal` file when restic reads the main file, so a restore
  can be internally inconsistent (realistic outcome: all users locked out). `restore-test.sh:9`
  only checks that `docker-compose.yml` and the Caddyfile exist; `restic check` is never run;
  the systemd unit has no `OnFailure`; CWA's `auto_backup_imports/conversions/epub_fixes`
  copy every processed file into `cwa/config/processed_books` (doubling storage on 80 GB)
  and those copies are backed up too. The 04:30 unattended reboot can land inside a long
  first backup (02:30 + 15 min + transfer).
- **How**: in `backup.sh`, before `restic backup`, snapshot each DB with the SQLite backup
  API via the host's `python3` (no new package): `python3 -c 'import sqlite3,sys; s=sqlite3.
  connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)' "$db"
  "$STACK_DIR/.backup-snap/<name>"` for the seven files above (skip missing ones); exclude
  `*.db-wal`, `*.db-shm`, `*.sqlite-wal`, `cwa/config/processed_books`, `library/staging`,
  `downloads`; keep `caddy/data/access.log` excluded (it is now redacted, N04, but still
  noise). After backup: `restic check` (structure) daily, `restic check
  --read-data-subset=5%` on Sundays; log `restic stats latest --json`. Set `auto_backup_
  imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0` in
  `apply_library_defaults` (toggle in `step_formats` for admins who want the copies).
  `restore-test.sh`: restore latest, `PRAGMA integrity_check == ok` on every snapshot DB,
  `SELECT COUNT(*) FROM user` > 0 in `app.db`, `SELECT COUNT(*) FROM books` ≥ 1 in
  `metadata.db`, FAIL if the snapshot is older than 36 h; document "restore = copy
  `.backup-snap/*` over the live files". `step_backup`: `OnFailure=bookstack-alert@backup.
  service` (see N13 for the alert helper), a monthly `bookstack-restore-test.timer`, and
  `OnCalendar=01:00` so backup and reboot never overlap. `selftest.sh`: latest snapshot age
  < 36 h.
- **Effort**: small. **Priority**: high.

### N13 — Disk watchdog, growers cleanup, minimal alert helper
- **What**: README says "watch `df -h /srv`"; nothing does. Growers on an 80 GB disk:
  `downloads/incomplete` and `library/staging` (never cleaned; the torrent watcher removes
  only matched EPUBs), CWA conversion temp, Docker build cache from the two on-box builds on
  every Update (`step_update` prunes images only), journald, restic's cache, audiobooks. When
  `/` fills, SQLite writes fail, CWA ingest wedges, Caddy cannot log and swap cannot grow —
  a cascading outage no restart fixes.
- **How**: `scripts/disk-watch.sh` from `/etc/cron.d` hourly: `pct=$(df --output=pcent
  "$STACK_DIR" | tail -1 | tr -dc 0-9)`; ≥ 85 → alert once per 24 h (state file in
  `/etc/bookstack`); ≥ 95 → `docker compose stop shelfmark aria2`, pause qBittorrent via API
  when credentials exist, alert at high priority; < 80 → resume. Always: `find
  downloads/incomplete library/staging -type f -mtime +14 -delete`, `find library/ingest
  -name '*.part' -mmin +1440 -delete`, `journalctl --vacuum-size=200M`, `docker builder
  prune -f --filter until=168h`, `restic cache --cleanup`. Alert helper: add a tiny CLI to
  `notify.py` (`python -m notify alert "<title>" "<text>"` → `NOTIFY_WEBHOOK` + `ADMIN_EMAIL`
  via the existing SMTP) and a `scripts/alert.sh` wrapper (`docker exec librarian …`, falls
  back to `logger`); a templated `bookstack-alert@.service` for `OnFailure=`. `step_update`:
  `docker system prune -f --filter until=72h` instead of `image prune`. `selftest.sh`: FAIL
  when free < 10 % or < 5 GB, warn when any file in `library/ingest` is older than 30 min
  ("ingest stuck") or `processed_books` > 5 GB. `/admin` already shows disk; colour it at
  85/95 and add the ingest backlog count from N09's `/healthz`.
- **Effort**: small. **Priority**: high.

### N14 — Pin image tags; make Update a safe operation
- **What**: `docker-compose.yml` pulls `:latest` for CWA, ABS, qBittorrent, aria2, AriaNg,
  Shelfmark and FlareSolverr; only Authelia (4.39) and Kuma (`:1`) are pinned;
  `caddy/Dockerfile` uses `caddy:2-builder`/`caddy:2`. `step_update` (`bookstack.sh:851-856`)
  pulls everything, restarts, prunes — no pre-update backup, no health gate, no rollback.
  CWA's 3.x → 4.0 jump carried schema migrations; Calibre 9 changed `metadata.db` columns and
  broke calibre-web installs; Shelfmark describes itself as feature-stable/best-effort.
- **How**: `IMG_*` variables in `.env` (seeded by `envdefault` to the release tags current
  when this is implemented — check Docker Hub/GHCR at that time) and `image: ${IMG_CWA}` etc.
  in all three compose files; pin `caddy:2.10-builder`/`caddy:2.10` and the three xcaddy
  modules by version (caddy-dns/cloudflare must be a release that accepts the new `cfut_`
  token format). Rewrite `step_update`: (a) `scripts/backup.sh --tag pre-update` — abort on
  failure; (b) save `docker compose config --images` to `.images.prev`; (c) show old → new
  tags in a `yesno`; (d) pull/build/up; (e) wait for every healthcheck ≤ 300 s then run
  `selftest.sh`; (f) on failure offer rollback (restore `.images.prev` values, `up -d`);
  (g) only then prune. Add "Check for updates" (`compose pull --dry-run`-style diff, links to
  CWA/ABS release notes). `tui-test.sh`: update refuses when the pre-update backup fails.
  Diun notifications are L06.
- **Effort**: small. **Priority**: high.

### N18 — Selftest: isolation invariants, Tailscale key expiry, fail2ban jail, Caddy admin, OOM, gate bypasses
- **What**: `selftest.sh:59` checks only `config_public_reg` and `config_kobo_sync`; CWA v4
  has `duplicate_auto_resolve_enabled` (can merge two users' copies of one title) and
  `auto_metadata_update_tags` (can replace `owner:<user>`), and `auto_ingest_automerge` is
  what isolation depends on — none is checked. `step_lock_ssh` closes port 22 while
  Tailscale node keys expire after 180 days by default (`tailscale up --ssh` at `:165`
  never disables expiry), a scheduled lock-out ~6 months after install with only the
  provider console left. Nothing checks that the fail2ban jail is active, that the Authelia
  gate still lets device paths through, or that a container was OOM-killed.
- **How**: "Isolation invariants" block reading `cwa.db`: FAIL if `auto_ingest_automerge
  != 'new_record'` or (when the columns exist) `duplicate_auto_resolve_enabled=1` or
  `auto_metadata_update_tags=1`; also print `auto_convert_ignored_formats` and
  `koreader_sync_enabled`. `tailscale status --json | jq -r .Self.KeyExpiry`: FAIL if port
  22 is not public and expiry < 30 days; `step_tailscale`/`step_lock_ssh` print "Disable key
  expiry for this machine in the Tailscale admin console" and require a yes. `fail2ban-client
  status caddy-auth` when installed. Caddy admin port (N04). `OOMKilled` (N11). When
  `forward_auth` is in the Caddyfile: `curl -o /dev/null -w %{http_code}
  https://audio.$D/ping` and `-u x:y https://books.$D/kosync/users/auth` must not be an
  Authelia 302 (N05). Cloudflare device probe (N10).
- **Effort**: small. **Priority**: high.

### L06 — Update notices (Diun) and in-app CWA update banner
- **How**: `crazymax/diun` service (docker socket read-only, watch-by-default, notify via
  the alert channel from L07 or e-mail), `cwa_update_notifications=1` in defaults.
- **Effort**: small. **Priority**: medium (later).

### L07 — Alert channel and Uptime Kuma bootstrap
- **What**: `step_monitoring` only prints instructions; no monitors, no notification
  provider, no push (dead-man) monitors for the backup timer or cf-ips cron.
- **How**: self-hosted `ntfy` on the tailnet (`auth-default-access: deny-all`, token in
  `.env`), `alert.sh` from N13 posts there first; `scripts/kuma-bootstrap.py` (uptime-kuma-api)
  idempotently creates the notification and monitors (public URLs, `/healthz` JSON,
  Shelfmark `/api/health`, container monitors, push monitors for backup/cf-ips/synthetic);
  `backup.sh`/`cf-ips.sh` end with a push. Decide `uptime-kuma:1` vs `:2` (in-place DB
  migration) in a maintenance window.
- **Effort**: medium. **Priority**: medium (later).

### L08 — Scheduled synthetic user journey on the VPS (canary user)
- **How**: `scripts/synthetic.py` reusing `tests/e2e_driver.py` helpers, as user `_canary`
  (hidden from user lists): login → upload a generated EPUB through Cloudflare → wait for
  import with the owner tag → download → second canary gets 404 → OPDS + Kobo init through
  Cloudflare → Shelfmark login → weekly Send-to-Kindle to the admin's own address → cleanup
  via `calibredb remove`. Timer twice daily; failures go to `alert.sh`; durations logged to
  `librarian.db` and shown on `/admin` (import latency is the earliest CWA-degradation
  signal).
- **Effort**: medium. **Priority**: medium (later).

### L09 — Certificate and token expiry watch
- **How**: daily `cert-watch.sh`: `openssl x509 -checkend` on every origin cert under
  `caddy/data/caddy/certificates` (alert < 14 days = renewal is failing), on
  `cf-origin-pull-ca.pem` (< 60 days), grep Caddy logs for `could not get certificate`;
  `cf-ips.sh` re-downloads the origin-pull CA and reloads Caddy if it changed; `selftest.sh`
  verifies the Cloudflare token with `/user/tokens/verify`.
- **Effort**: small. **Priority**: medium (later).

### L20 — Restart unhealthy containers (autoheal)
- **What**: `restart: unless-stopped` only reacts to PID 1 exiting; Docker never acts on a
  failing healthcheck, so a wedged CWA stays "unhealthy" forever.
- **How**: `willfarrell/autoheal` with the docker socket read-only and the `autoheal` label
  on calibre-web, librarian, shelfmark, audiobookshelf; add healthchecks to audiobookshelf
  (`/healthcheck`) and qbittorrent (`/api/v2/app/version`). N09 makes the portal's
  healthcheck honest first. The socket mount is root-equivalent for that container — accept
  it consciously or skip.
- **Effort**: small. **Priority**: medium (later).

### L02 — Make the P2P path actually work (qBittorrent auth, temp path, ingest mounts)
- **What**: `config.py:114-118` assumes "bypass auth for localhost"; the librarian's
  connection to the published port arrives from the Docker bridge gateway, not 127.0.0.1, so
  even that setting would not match and `Qbit().add()` gets 403. The LSIO image prints a new
  temporary password on every restart until one is set. qBittorrent and aria2 mount
  `./library/ingest` directly (`docker-compose.yml:81,101`) and write in place, so CWA can
  import half-written files. No temp path → the torrent watcher can pick up partial EPUBs.
- **How**: pre-seed `qbt/config/qBittorrent/qBittorrent.conf` before first start
  (`WebUI\ServerDomains=dl.<domain>`, `WebUI\Password_PBKDF2` from a generated
  `QBIT_PASS` stored in `.env` and passed to librarian, `Session\TempPathEnabled=true`,
  `Session\TempPath=/downloads/incomplete`, ratio/seed-time limits, "Disable OS cache");
  remove the ingest mounts (qB keeps staging + incomplete; aria2 downloads to
  `downloads/incomplete` and moves finished files to `library/dropbox/admin`); selftest
  calls `torrents/info` with the stored credentials. `IA_USE_TORRENT` is off by default, so
  this is not a go-live blocker.
- **Effort**: medium. **Priority**: medium (later).

### L03 — Torrent watcher: match by hash, not by substring or "oldest pending"
- **What**: `worker.py:237-261` globs every `*.epub` under staging every 20 s, matches a
  request by identifier substring **or falls back to `pending[0]`** (any user's oldest
  torrent), ingests partial files, deletes qBittorrent's live file, and never times out.
- **How**: `Qbit.add(url, save_path, tag=f"req-{rid}")` and store the hash; poll
  `/api/v2/torrents/info?category=owned-staging`, act only on `progress == 1.0`, walk
  `content_path`, ingest every EPUB under the tagged request's owner, then delete the
  torrent with files; `db.expire_downloading(24h)`. Unit test with a fake qB HTTP server.
  Depends on L02.
- **Effort**: medium. **Priority**: medium (later).

### L21 — Asynchronous Send-to-Kindle
- **What**: `send_kindle` is synchronous; a slow relay with a 45 MB attachment can hit
  Cloudflare's 100 s proxy timeout (524) even though the mail is sent.
- **How**: enqueue a `kindle` job row processed by the worker; flash "Sending… see Status".
- **Effort**: small. **Priority**: low (later).

### L22 — aria2 downloads should enter through a dropbox
- **How**: mount `./library/dropbox/admin` as aria2's `/downloads` (or `downloads/incomplete`
  + an `--on-download-complete` move) so admin downloads go through the atomic, tagged path
  instead of landing raw in CWA's watched folder. Folded into L02 if done together.
- **Effort**: small. **Priority**: low (later).

---

## 7. Devices & formats

### N05 — Authelia/Caddy gate breaks Audiobookshelf apps, KOReader sync and the intake webhook
- **What**: `authelia/configuration.yml.template:19-31` bypasses only `books.<d>/kobo/*` and
  `/opds*`; `audio.` and `request.` are `two_factor` with no exceptions, and
  `caddy-gate.snippet:1` applies the same `not path /kobo/* /opds /opds/*` matcher to all
  four vhosts. The Audiobookshelf mobile apps (and Plappa/ShelfPlayer/Lissen) authenticate
  with ABS's own token against `/login`, `/api/*`, `/socket.io`, `/hls/*`, `/s/*`, `/ping`
  and cannot follow a forward-auth redirect; CWA's KOSync uses HTTP Basic on `/kosync/*`;
  `POST request.<d>/intake` gets an Authelia redirect, silently killing the automation
  feature. So the moment the documented "Security → Authelia" step runs, the Devices page's
  promises ("install the Audiobookshelf app", "KOReader progress sync") stop working. The
  Cloudflare Access guide (`bookstack.sh:665-666`) has the same gap.
- **How**: per-vhost bypass lists. Either two snippets or one snippet with a `@@BYPASS@@`
  placeholder filled by `inject_authelia_gate` per marker (`# @AUTHELIA_GATE:books@`,
  `:audio@`, `:request@`, `:shelf@` in the Caddyfile template): books `/kobo/* /opds /opds/*
  /kosync /kosync/*`; audio `/login /logout /api/* /socket.io/* /hls/* /s/* /ping /status
  /healthcheck /public/* /feed/*`; request `/intake /healthz`; shelf none. Mirror the same
  resources as `bypass` rules per domain in the Authelia template, placed before the
  `two_factor` rule. Accept and document the consequence: ABS's own login remains reachable
  for anyone with ABS credentials (same trade as Kobo/OPDS). Update `step_cfaccess`,
  README "Authelia notes", `tests/tui-test.sh:114-115`, `tests/caddy-test/Caddyfile` and
  e2e §12 (assert `audio /ping` and `books /kosync/users/auth` are not redirected).
- **Effort**: small. **Priority**: high.

### N07 — Send-to-Kindle format rules, honest format labels, OPDS URL
- **What**: `send_kindle` (`app.py:296-311`) mails whatever `preferred_format` resolves to
  and `kindle.py:3` claims AZW3/MOBI are accepted; Amazon's Send-to-Kindle accepts EPUB, PDF,
  DOC/DOCX, TXT, RTF, HTML and images only (MOBI/AZW were dropped in 2022, AZW3 was never
  accepted), with a 50 MB / 25-attachment mail limit and the approved-sender list. The
  Devices label "azw3 (Kindle)" actively steers users into a silent bounce; "kepub (Kobo)"
  never exists in the library (see L11), so `best_format` falls back to EPUB. Devices prints
  the OPDS URL without the trailing slash KOReader needs for calibre-web. There is no way to
  test the approved-sender step before the first real book.
- **How**: `config.KINDLE_FORMATS=("epub","pdf")`; `kindle.send` raises for other
  extensions; `send_kindle` and `_auto_kindle` pick `epub`, else `pdf`, else flash "no
  Kindle-compatible format yet"; relabel `azw3` "(Kindle via USB only)", `mobi` "(legacy)",
  remove or relabel `kepub`; add a Devices action `kindle_test` calling the existing
  `kindle.send_test(addr)` and store `last_kindle_test` in prefs; print `/opds/` with the
  slash on Devices and README; note the 50 MB limit next to the address field. Tests:
  azw3-only book refused; epub+azw3 with pref azw3 sends the epub.
- **Effort**: small. **Priority**: high.

### N10 — Cloudflare edge settings vs devices, and the upload cap
- **What**: README:150 and `step_cloudflare`'s message (`bookstack.sh:250`) tell the admin to
  turn Bot Fight Mode ON and, if Kobo breaks, "add a WAF skip rule for /kobo/*". Cloudflare
  documents that Bot Fight Mode "may challenge API or mobile app traffic" and that "you
  cannot bypass or skip Bot Fight Mode using WAF custom rules or Page Rules" — it runs
  outside the ruleset engine. Kobo firmware, KOReader, OPDS apps and the ABS apps are
  exactly the non-browser clients it challenges, and the failure is silent on the device.
  `step_cloudflare` also sets `browser_check on` (Browser Integrity Check, which challenges
  non-standard user agents); that one *can* be disabled per path with a Configuration Rule.
  `MAX_UPLOAD_MB` defaults to 200 (`config.py:29`, `docker-compose.yml:163`) but Cloudflare's
  request-body limit is 100 MB on Free and Pro, so uploads between 100 and 200 MB die at the
  edge with a 413 the portal never sees. `selftest.sh:69-71` only checks the root URL, which
  passes while `/kobo` is challenged.
- **How**: (1) remove the Bot Fight Mode advice from README:150, `DEPLOYMENT-CHECKLIST.md:53`
  and `bookstack.sh:250`; say plainly that it breaks Kobo/OPDS/KOReader/ABS apps and cannot
  be exempted on Free (Super Bot Fight Mode with a skip rule needs Pro). (2) In
  `step_cloudflare`, create a Configuration Rule via the rulesets API (`http_config_settings`
  phase, action `set_config` with `bic: false`) for `books.<d>` paths `/kobo/`, `/opds`,
  `/kosync` and `audio.<d>` paths `/api/`, `/socket.io`, `/hls/`; the token needs the
  Config Rules edit permission (add it to the permission list shown in `step_configure` and
  README); fall back to printing the dashboard steps if the API refuses. Keep
  `browser_check on` for the HTML apps. (3) `MAX_UPLOAD_MB` default 95 in `config.py`,
  compose and `.env.example`; show the limit on `upload.html`; add a 413 handler with a
  friendly message and pointer to the dropbox/Tailscale path (L18 later). (4)
  `selftest.sh`: `curl -A 'Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/533.1
  (KHTML, like Gecko) Version/4.0 Mobile Safari/533.1 Kobo'
  https://books.$D/kobo/$(admin token)/v1/initialization` → PASS only on 200 with
  "Resources"; `https://books.$D/opds/` without credentials → 401 with `WWW-Authenticate`
  (proves CWA answered, not a challenge page); FAIL on a `cf-mitigated` header.
- **Effort**: small. **Priority**: high.

### N19 — Non-Latin filenames are rejected on upload and mangled elsewhere
- **What**: `app.upload` (`app.py:255-259`) runs `secure_filename` on the whole name and then
  derives the extension from the result; Werkzeug drops every non-ASCII character, so
  `Война и мир.epub` becomes `epub`, `युद्ध और शांति.pdf` becomes `pdf`, the extension is
  empty and the user is told "That file type is not supported". `worker._safe`
  (`worker.py:19-20`) and `imap._SAFE` map every non-`[A-Za-z0-9 ._-]` character to `_`, so
  PDFs from a Hindi or Russian dropbox import as `________ - ________`. The owner's domain
  and timezone suggest Devanagari titles will be routine.
- **How**: `stem, ext = os.path.splitext(f.filename)`; validate `ext` first; `stem =
  _safe(stem) or f"upload-{uuid4().hex[:8]}"`. `_safe`: `unicodedata.normalize("NFC")`, strip
  only `\\ / : * ? " < > |` and control characters, trim to 150 chars, fall back to `book`.
  Same for `imap._SAFE`. Tests with a Devanagari and a Cyrillic filename in `test_app.py`
  and `test_core.py`.
- **Effort**: small. **Priority**: medium.

---

## 8. Code audit findings

### N08 — The owner-tagger re-zips every EPUB uncompressed and drops unusual OPF paths
- **What**: `tagger.add_owner_tag` (`tagger.py:34-40`) opens `zipfile.ZipFile(tmp, "w")` —
  default `ZIP_STORED` — and calls `writestr(name, bytes)` for every entry, discarding the
  original `ZipInfo`. Every XHTML/CSS/font entry is therefore stored raw: a text-heavy book
  grows 3–5×, a small test EPUB 300×, an illustrated 30–40 MB EPUB crosses
  `KINDLE_MAX_MB=45` and Amazon's 50 MB limit, Kobo syncs and downloads slow down, and disk
  fills faster. The existing test only checks that `mimetype` is stored. `_opf_path` returns
  `container.xml`'s `full-path` verbatim; a URL-encoded or case-mismatched path raises
  `KeyError`, which `_atomic_ingest_epub` swallows as "tag skipped" and imports untagged — for
  a non-admin that is a book they cannot see.
- **How**: iterate `zin.infolist()`; `zout.writestr(info, data)` with the original
  `ZipInfo` (preserves `compress_type`, timestamps, attributes; `mimetype` stays stored
  because its info says so); write the modified OPF with `compress_type=ZIP_DEFLATED`.
  Resolve the OPF with `urllib.parse.unquote` and a case-insensitive fallback over
  `namelist()`; raise `TagError` when not found. In `_atomic_ingest_epub`, for non-admin
  owners treat `TagError` as failure (park the file in `.failed/`, status `error: could not
  embed owner tag`) instead of importing untagged. Tests: size after tagging within 1.1× of
  before, per-entry `compress_type` preserved, an EPUB with `OEBPS/content%20x.opf`.
- **Effort**: small. **Priority**: high.

### Smaller items folded into other ids
- Cover proxy (`app.py:187-199`) trusts the upstream `Content-Type` and has no size cap:
  `stream=True`, read ≤ 2 MB, only return when `Content-Type` starts with `image/`. Fold
  into N17.
- `MAX_ZIP_UNPACKED` (8 GiB) is larger than the free space an X4 will usually have: add a
  `shutil.disk_usage` headroom check (free − 2 GB) before extraction. Fold into N13.
- Two stale TUI texts (`bookstack.sh:565,570,789`) and README:199-200 say audiobooks/non-EPUB
  are tagged by hand or "before import". Fixed by N06/N20.
- `worker.py`'s `UA` string says `bookstack-librarian/3.0`; `abs.py` says 4.0. Cosmetic.

---

## 9. Considered and rejected

- **CrowdSec bouncer in Caddy instead of fail2ban.** v4.1's fail2ban already bans the real
  client at the Cloudflare edge; CrowdSec adds a container, an xcaddy module and enrolment on
  a 4 GB box for marginal gain at this user count. Revisit if the audit log shows sustained
  brute force.
- **Storyteller (read-along alignment).** Whisper transcription needs RAM/CPU the X4/X8 do
  not have to spare; out of scope for this repo.
- **Watchtower / automatic image updates.** Archived upstream; unattended major bumps are
  the most likely way this stack breaks. Diun (notify only) + the guarded Update flow instead.
- **rclone / object-storage mount for audiobooks.** ABS inode churn forces rescans,
  qBittorrent cannot write to FUSE, Shelfmark's destination needs a local VFS cache; buy the
  X8's disk (or a block volume) instead.
- **Rootless Docker / `userns-remap`.** PUID 1000 bind mounts and the LSIO images make this a
  multi-day change with little benefit over L01's `cap_drop`/networks.
- **Bundling Calibre into the portal image to convert non-EPUB before tagging.** Adds a
  second 2.5 GB Calibre and doubles conversion load; pre-import tags for PDF/CBZ (N06) and
  CWA's converter cover the common cases, L10 the rest.
- **Replacing the pre-import tagger with post-import writes to `metadata.db`.** A second
  writer against Calibre's database for every book; kept only as the narrow fallback in L10.
- **Cloudflare Access instead of Authelia.** Already documented as the alternative; run one.
- **Bot Fight Mode.** Breaks non-browser clients and cannot be exempted on the Free plan.
- **Running Ephemera on X4.** Unmaintained upstream plus ~1 GB for FlareSolverr and an on-box
  Node build; X8 only, and only if its request-and-wait queue is really wanted.
- **X2 plan.** Not viable; **X16** only for disk.
- **Kobo store proxy on.** Privacy; users are told the store/Libby stop working while linked.

---

## 10. Sources

Repo (all paths under `/Users/kenith.philip/booky`): `README.md`, `MANIFEST.md`,
`docker-compose.yml`, `docker-compose.authelia.yml`, `docker-compose.ephemera.yml`,
`bookstack.sh`, `caddy/Caddyfile.template`, `caddy/Dockerfile`,
`authelia/configuration.yml.template`, `authelia/caddy-gate.snippet`,
`configs/fail2ban/jail.local`, `configs/fail2ban/caddy-auth.conf`, `librarian/*.py`,
`librarian/templates/*.html`, `librarian/tests/fixtures/*.sql`, `scripts/*.sh`,
`tests/e2e_driver.py`, `tests/tui-test.sh`, `tests/docker-compose.test.yml`,
`docs/DEPLOYMENT-CHECKLIST.md`.

Cloudflare
- Request body limits per plan (100 MB Free/Pro): https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/
- Bot Fight Mode cannot be skipped by WAF rules; may challenge API/mobile traffic: https://developers.cloudflare.com/bots/get-started/bot-fight-mode/
- Browser Integrity Check: https://developers.cloudflare.com/waf/tools/browser-integrity-check/
- Authenticated Origin Pulls (shared CA): https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/
- 524 timeouts: https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/
- Using Cloudflare to bypass Cloudflare (Certitude): https://certitude.consulting/blog/en/using-cloudflare-to-bypass-cloudflare/

Caddy
- Admin API: https://caddyserver.com/docs/api
- Caddyfile global options / placeholders: https://caddyserver.com/docs/caddyfile/options and https://caddyserver.com/docs/caddyfile/concepts
- `log` directive, `filter` encoder, `client_ip` vs `remote_ip`: https://caddyserver.com/docs/caddyfile/directives/log
- caddy-dns/cloudflare token format fix: https://github.com/caddy-dns/cloudflare/pull/123
- qBittorrent behind Caddy (Host header): https://github.com/qbittorrent/qBittorrent/wiki/Linux-WebUI-HTTPS-with-Let's-Encrypt-&-Caddy2-reverse-proxy

Calibre-Web Automated / Calibre
- Repository and v4 release notes (auto-send, metadata automation, duplicate scanner, KOReader sync): https://github.com/crocodilestick/Calibre-Web-Automated and https://github.com/crocodilestick/Calibre-Web-Automated/discussions/941
- KOReader synchronisation: https://deepwiki.com/crocodilestick/Calibre-Web-Automated/5.1-koreader-synchronization
- Kobo integration and sync: https://github.com/crocodilestick/Calibre-Web-Automated/wiki/Kobo-Integration-&-Sync
- Reverse-proxy header authentication: https://github.com/crocodilestick/Calibre-Web-Automated/wiki/Reverse-Proxy-Authentication
- Ingest lock / DB-lock regressions: https://github.com/crocodilestick/Calibre-Web-Automated/issues/1256, https://github.com/crocodilestick/Calibre-Web-Automated/issues/1082
- Comic conversion quality: https://github.com/crocodilestick/Calibre-Web-Automated/issues/1098
- Calibre PDF metadata reader (Keywords → tags): https://raw.githubusercontent.com/kovidgoyal/calibre/master/src/calibre/ebooks/metadata/pdf.py
- Calibre archive reader (ComicBookInfo zip comment): https://raw.githubusercontent.com/kovidgoyal/calibre/master/src/calibre/ebooks/metadata/archive.py
- Calibre 9 metadata.db change breaking calibre-web: http://davidroessli.com/logs/calibre-9-broke-my-calibre-web-server/
- calibre-web Allowed/Denied tags: https://github.com/janeczku/calibre-web/wiki/Allowed-and-Denied-Tags
- calibre-web Kobo integration (no PDF sync): https://github.com/janeczku/calibre-web/wiki/Kobo-Integration
- Kobo behind Cloudflare challenges: https://github.com/janeczku/calibre-web/issues/2901

Audiobookshelf
- API: https://api.audiobookshelf.org/
- User management / tag restriction: https://audiobookshelf.org/docs/documentation/server-management/user-management/
- Forward auth breaks the mobile app: https://github.com/advplyr/audiobookshelf/discussions/809
- Scan memory: https://github.com/advplyr/audiobookshelf/issues/2793
- Community apps: https://audiobookshelf.org/docs/documentation/community/community-apps/

Shelfmark
- Readme (RAM guidance, request system, auth modes): https://github.com/calibrain/shelfmark

Amazon / Kobo / KOReader
- Send to Kindle supported formats and limits: https://www.amazon.com/gp/help/customer/display.html?nodeId=G7NECT4B4ZWHQ8WV
- KOReader OPDS (trailing slash for calibre-web): https://github.com/koreader/koreader/wiki/OPDS-support
- kepubify: https://pgaskin.net/kepubify/

Security advisories
- gunicorn CVE-2024-6827 (fixed 23.0.0): https://security-tracker.debian.org/tracker/CVE-2024-6827
- requests CVE-2024-47081 (fixed 2.32.4): https://github.com/psf/requests/releases and https://access.redhat.com/security/cve/cve-2024-47081
- Flask CVE-2025-47278 (affects 3.1.0 only; fixed 3.1.1): https://security-tracker.debian.org/tracker/CVE-2025-47278
- OWASP unvalidated redirects: https://cheatsheetseries.owasp.org/cheatsheets/Unvalidated_Redirects_and_Forwards_Cheat_Sheet.html

Operations
- Tailscale key expiry (180-day default): https://tailscale.com/kb/1028/key-expiry
- restic check / repository maintenance: https://restic.readthedocs.io/en/latest/045_working_with_repos.html
- restic + B2 append-only patterns: https://helgeklein.com/blog/restic-encrypted-offsite-backup-with-ransomware-protection-for-your-homeserver/
- SQLite backup API: https://www.sqlite.org/backup.html
- Python zipfile (extraction safety): https://docs.python.org/3/library/zipfile.html
- fail2ban cloudflare-token action: https://github.com/fail2ban/fail2ban/blob/master/config/action.d/cloudflare-token.conf
- Docker does not restart unhealthy containers / autoheal: https://github.com/willfarrell/docker-autoheal
- Diun: https://crazymax.dev/diun/
- ntfy configuration: https://docs.ntfy.sh/config/
- Uptime Kuma v1 → v2 migration: https://github.com/louislam/uptime-kuma/wiki/Migration-From-v1-To-v2
- qBittorrent memory behaviour in Docker: https://ryansouthgate.com/fixing-qbitorrent-in-docker-oom/
- FlareSolverr resource guidance: https://github.com/FlareSolverr/FlareSolverr/issues/1123
- Bash `set -e` semantics in `||` lists: https://www.gnu.org/software/bash/manual/html_node/The-Set-Builtin.html
- whiptail stdout/stderr behaviour: https://en.wikibooks.org/wiki/Bash_Shell_Scripting/Whiptail
- Werkzeug `secure_filename` (ASCII-only): https://raw.githubusercontent.com/pallets/werkzeug/3.0.3/src/werkzeug/utils.py
