# mfdata.in private library — hardened, per-user, self-serve (v4)

A private book/audiobook library on one small VPS. **Admins** install and run everything from
one menu (`bookstack.sh`). **Users** only ever see the portal: they sign in, search, request
or upload a book, and it appears on their Kobo, their Kindle, their phone, or as a direct
download — only their own books, never anyone else's. Nothing is public.

## What you get

| For end users (`request.<domain>`) | For the admin (`bookstack.sh`, plus `/admin` in the portal) |
|---|---|
| Search the curated catalogs and **Request** (with optional admin approval) | **Quick install**: system → Tailscale → Cloudflare → deploy → backups, in order |
| **Upload** a book they own | **Users & devices**: create an isolated account, Kobo link, Kindle address, passwords — one screen |
| **My books**: download in their preferred format, or **Send to Kindle** | **Library**: target format & conversion policy, SMTP for Send-to-Kindle, sources, Shelfmark, intake |
| **Devices**: generate their Kobo sync link, set their Kindle address, pick a format, opt into auto-send | **Security**: Authelia SSO + 2FA gate, SSH lock, fail2ban, SPF/DMARC |
| Extended search at `shelf.<domain>` (Shelfmark) with the same login | **Operations**: self-test, status, logs, update, restore test, Ephemera |

Everything an admin previously had to click through in three different web UIs (user
creation, Allowed Tags, Kobo sync toggle, registration off, conversion settings, Kindle
addresses) is now done by the menu or by users themselves in the portal.

## What changed in v4.2 (research sweep + adversarial review)
Twenty pre-deploy items from `docs/RESEARCH-GAPS.md` (what comparable self-hosted stacks
do, what this one lacked) were implemented, then three independent reviewers tried to break
the result; every blocker they found is fixed and covered by a test.
- **Exposure order.** Deploy starts Caddy *last*: only after the admin-gate password exists
  and Audiobookshelf has its root user, so nobody can claim `audio.<domain>` first. An empty
  or malformed admin hash refuses to render the Caddyfile instead of taking every site down.
- **Trust boundaries.** The worker only fetches URLs on each source's allowlist and refuses
  private, loopback and CGNAT targets (also after redirects); e-mail and webhook intake check
  that the user exists and that the sender is allowed (`IMAP_ALLOWED_SENDERS`); Caddy's admin
  API lives on a unix socket and the Cloudflare token is read at runtime, never written into
  the config; every `Remote-*` header a client sends is stripped before the Authelia gate.
- **Device paths through the gate.** Audiobookshelf apps, KOReader sync, OPDS, Kobo and the
  intake webhook bypass Authelia via anchored, case-sensitive regexps (one list in
  `authelia/inject-gate.py`, mirrored in the Authelia rules); each is rate-limited per client.
- **Uploads that stay visible.** PDF (Info + XMP) and CBZ get the owner tag before import
  and are excluded from auto-conversion; MOBI/AZW3/FB2/TXT park in a `needs-tag` state the
  admin resolves; unsupported files and any symlink planted in a dropbox are parked under
  `.failed/` and reported, never followed or deleted. Non-Latin filenames are kept.
- **Kindle correctness.** Send-to-Kindle only mails EPUB/PDF/TXT (Amazon rejects the rest);
  the upload cap is 95 MB (Cloudflare's body limit); EPUBs keep their compression. The two
  repairs Amazon most often bounces a file for (missing `dc:language`, XHTML without an
  encoding declaration) are applied by the portal to the mailed copy. CWA's own import-time
  "Kindle EPUB fixer" ships OFF: it rewrites every archive it is handed, including CBZ, and
  drops the zip comment that is the only place Calibre reads comic metadata from, so comics
  lost their owner tag and vanished from the uploader's view (found by the end-to-end run).
  If an admin turns it back on, comic uploads are reported as `needs-tag` instead of silently
  importing untagged.
- **Resilience on a small box.** Memory and PID fences per container, hourly disk watchdog
  that stops the downloaders at 95 %, consistent SQLite copies in backups, a restore test that
  restores config + DB snapshots beside the stack (not into tmpfs), `Update` that backs up
  first, bumps pinned tags (`IMG_*`), waits for health, self-tests and offers rollback.
- **Session safety.** A transient app.db error no longer logs every user out; account
  changes still revoke within 5 minutes. Session secret rotation from the Security menu.
- **Logs without secrets.** Access logs redact Kobo tokens and `?token=` JWTs and drop
  Authorization, Cookie, intake and KOReader headers; fail2ban keys on the real client IP.

## What changed in v4.1 (hardening + audiobooks)
- **Audiobooks are now as automatic as ebooks.** `Library → Audiobookshelf` bootstraps ABS
  (root user, permanent API key, library) in one step; every new library user gets an ABS
  account restricted to `owner:<user>`; the worker scans and tags each new audiobook to its
  owner within a minute. Passwords stay in step across the portal, CWA, Shelfmark and ABS.
- **Brute-force lockout** in the portal (per user+IP and per IP, real client IP via Caddy),
  **audit trail** on the admin page (logins, lockouts, requests, downloads, admin actions),
  **daily request quota** per user, **CSP** and anti-framing headers, 12-hour sessions,
  failed logins answer 401 so they are countable.
- **fail2ban fixed.** The old Caddy jail keyed on `remote_ip` (Cloudflare's edge behind the
  proxy) and counted every 401 (including the normal OPDS challenge), so a burst of failed
  logins could have banned Cloudflare and locked everyone out. It now keys on the real
  client IP, counts only POSTs to login endpoints, and bans through a Cloudflare IP Access
  Rule (needs the token permission *Firewall Services: Edit*).
- **Self-service**: users change their own password on Devices; opt into e-mail when a
  request is ready or denied; the admin is e-mailed when a request awaits approval.
- **KOReader sync** toggle (Library → Formats) with instructions on the Devices page.
- **Rate limits** on `/download/*` and `/intake` at Caddy; safe zip extraction (no traversal,
  size caps) for audiobook archives; e-mail intake supports plain IMAP for local relays.
- **Ephemera** overlay carries its own build recipe (pinned pnpm; the fork's Dockerfile no
  longer builds) and is verified to start.
- **Tests**: the end-to-end run now also covers a simulated Kobo device sync, the intake
  webhook, Shelfmark-style dropbox drops, Send-to-Kindle and e-mail intake against a real
  mail server, the Authelia gate with the production snippet, the whole Audiobookshelf path,
  lockout/quota/audit/notifications, and reports peak memory per container.

## What changed in v4
- **One TUI, nested menus.** `Install & deploy`, `Users & devices`, `Library`, `Security`,
  `Operations`. A **Quick install** runs the whole first-time setup in order.
- **Users are created from the menu (or the admin page)** with the isolation tag, dropbox,
  Kobo token and optional Authelia login in one step. No more GUI checklist per user.
- **Devices page** in the portal: Kobo sync link (the same token CWA's own button makes),
  Kindle address (stored in CWA, so CWA's Send-to-Kindle uses it too), preferred download
  format, and "auto-e-mail every new book to my Kindle".
- **My books** in the portal: direct download of the user's own books straight from the
  Calibre library (tag-scoped, path-confined), and Send-to-Kindle via the portal's own SMTP.
- **Formats & conversion menu** drives CWA's real settings (`cwa.db`): target format,
  convert-on-import, Kindle EPUB fixer, retained originals, and the duplicate policy that
  isolation depends on. Secure defaults are applied automatically on deploy.
- **Shelfmark** (the CWA companion) at `shelf.<domain>`, logging in with the same accounts and
  filing downloads into the user's dropbox so they get tagged like everything else.
- **Ephemera** as an optional Tailscale-only overlay (upstream is gone; see below).
- **Security fixes**: CSRF tokens on every form, `.env` values quoted so a `$` in a password
  no longer gets eaten by compose, single worker process so a request can't be processed
  twice, hidden/partial files skipped by the dropbox watcher, and a v3 bug fixed where
  **enabling Authelia truncated the Caddyfile** (taking every site down).
- **Tests**: unit tests run inside the shipping image against the real CWA/Calibre schemas,
  an installer harness, and a Docker end-to-end UAT that drives the real containers.

## The provider model (Discovery + Pull)
The portal is a federated search-and-grab frontend over a modular provider backend. A unified
search bar queries every enabled provider; each provider is a concrete adapter
(`librarian/fetchers.py`) returning a common result shape; **Request** pulls the file through
the acquisition engine, tags it to the user, and syncs it. OPDS catalog search-and-grab is
one of these adapters (`librarian/opds.py`).

**Adding a source** is a drop-in: write a `search(query)->list[dict]` adapter for that
source's real API, register it in `_ADAPTERS`, add a `config.SOURCES` flag. Nothing on the
frontend changes. Each adapter is bound in code to one specific, vetted source.

## Sources — read this first
The **request portal** (`request.`) fetches only from catalogs free to redistribute:
**Project Gutenberg, Standard Ebooks, Internet Archive (collections you allow), LibriVox**
(audio), plus **your own OPDS catalog**. Internet Archive also publishes torrents, so the
P2P path is real. The portal's source set is fixed in code — it is not a general indexer.

**Shelfmark** (`shelf.`) is a separate, general search-and-download UI. It ships with *no*
release source enabled; you pick them in its Settings (direct-download mirrors, Prowlarr
indexers, newznab, IRC, debrid/torrent clients). What you enable there, and what your users
pull through it, is your call and your responsibility under your local law and the terms of
each source. Both tools feed the same per-user pipeline, so isolation is identical.
Seed/serve only public-domain / Creative-Commons / your-own material.

For your own writing you don't need a tracker: the **OPDS source** (Library → Sources) and
**dropboxes / Syncthing → `library/dropbox/<user>`** (Library → Intake) are built in.

## What is exposed, and to whom

| Address | Who reaches it | Layers in front |
|---|---|---|
| request / books / audio / shelf .mfdata.in | Your users, anywhere | Cloudflare DDoS+WAF+bot → mTLS-locked origin → **(optional) Authelia SSO+2FA** → per-user app login |
| auth.mfdata.in | Users (only when Authelia on) | The SSO portal itself |
| dl / aria / monitor .mfdata.in | You, only on Tailscale | Not on the public internet → password gate → app login |
| ephemera.mfdata.in (optional) | You, only on Tailscale | Not on the public internet → password gate (Ephemera has no login of its own) |
| SSH | You, only on Tailscale (after locking) | Key-only |
| Port 6881 | Torrent peers | qB peer port only — no UI, no files |

Origin locked three ways (IP not in public DNS as an origin, firewall accepts 80/443 only
from Cloudflare, Caddy requires Cloudflare's client cert). Every container binds 127.0.0.1
with `no-new-privileges`. No anonymous browsing, no self-registration, no third-party
scripts, no telemetry. In the portal: CSRF tokens on every form, HttpOnly/Secure/Lax
sessions that expire after 12 h, a strict Content-Security-Policy (no scripts at all),
brute-force lockout per user+IP and per IP, a daily request quota, an audit trail, and
tag-scoped path-confined downloads. At Caddy: login, download and intake rate limits per
real client IP; optionally fail2ban bans repeat offenders at Cloudflare. Authelia adds
SSO + TOTP/passkey 2FA in front of everything but the device endpoints.

## Per-user isolation
One library, per-user **visibility** (not separate storage — admin sees all files):
- Every path that adds a book tags it `owner:<username>` inside the EPUB before import:
  portal requests and uploads, dropboxes, e-mail intake, the intake webhook, Shelfmark and
  Ephemera downloads (both routed through dropboxes).
- Every non-admin account has CWA **Allowed Tags = owner:<username>**, set automatically
  when the account is created (Users & devices → Add, or the portal's admin page) and
  re-applied by Users & devices → Repair. CWA enforces it everywhere — web, OPDS, Kobo sync,
  Send-to-Kindle — and the portal enforces the same tag on My books / downloads.
- The duplicate policy stays `new_record` (Library → Formats) so users' copies stay separate.
- **Audiobooks:** after `Library → Audiobookshelf`, every ABS account created from the Users
  menu (or the portal's admin page) is restricted to `owner:<username>` and the worker tags
  each new audiobook to its owner automatically. Without that step the request detail tells
  the admin which tag to set by hand.

## Before you deploy: accounts and the Cloudflare token
Everything below is free. The installer asks for each value when it needs it, so nothing has
to be edited by hand.

**1. Cloudflare (your domain must use Cloudflare's nameservers).**
Create the token at **dash.cloudflare.com → My Profile → API Tokens → Create Token →
Create Custom Token** and add these permissions, all with type **Zone**:

| Permission | Level | Why the installer needs it |
|---|---|---|
| Zone | Read | find your domain's zone |
| DNS | Edit | create the `books`, `audio`, `request`, `shelf`, `auth`, `dl`, `aria`, `monitor` records; also DNS-01 certificates |
| Zone Settings | Edit | SSL Full (strict), TLS 1.2+, HTTPS forced, Authenticated Origin Pulls, e-reader-breaking features off |
| Config Rules | Edit | per-host exceptions for the device paths |
| Cache Rules | Edit | never cache a user's book or audio response |
| Firewall Services | Edit | fail2ban bans abusive visitors at Cloudflare |

Under **Zone Resources** pick *Include → Specific zone → your domain*. Leave client IP
filtering empty (the server's IP changes if you ever rebuild) and set no expiry, or put a
reminder in your calendar. Copy the token once; Cloudflare will not show it again.
Paste it when **Quick install → Configure** asks for it. It is stored only in
`/srv/bookstack/.env` (root-only, mode 600) and read by Caddy at runtime. To replace it later,
re-run **Install & deploy → Configure** and paste the new one.

In the Cloudflare dashboard, turn **Security → WAF → Managed rules ON**. Leave **Bot Fight
Mode OFF**: it challenges Kobo, OPDS, KOReader and the Audiobookshelf apps, cannot be
exempted on the Free plan, and the devices fail silently.

**2. Tailscale.** Create an account and install the app on the devices *you* administer
from. The free Personal plan is enough: only you and the server join the tailnet. End users
never need Tailscale; they use the public sites through Cloudflare. **Quick install →
Tailscale** prints a login link for the server.

**3. Outgoing mail (optional, for Send-to-Kindle and notifications).** An SMTP account the
server sends from, set once by you in **Library → Mail**. Any provider that gives SMTP
credentials works (a Gmail or Fastmail app password, Brevo, Mailgun, your domain's mail host).
Users do not configure mail. Each user does two things themselves: enter their Kindle
address on the portal's **Devices** page, and add the server's *From* address (shown on that
page) to **Amazon → Manage Your Content and Devices → Preferences → Personal Document
Settings → Approved Personal Document E-mail List**. The **Send a test to my Kindle** button
proves it works.

**4. A backup repository off the server (recommended).** Any restic repository: an S3
bucket (Backblaze B2, Wasabi, Cloudflare R2) or an SFTP host. **Quick install → Backups**
asks for the repository URL, a password and the access keys. Keep the password somewhere
other than the server; without it the backups cannot be restored.

**5. Shelfmark release sources.** Nothing is enabled by default. After deploy, open
`shelf.<domain>` as admin → **Settings** and choose the sources you are entitled to use.

## Deploy (fresh Debian VPS)
Provision Debian (newest), hostname e.g. `lib01.mfdata.in`, paste your SSH key.
```
scp -r booky root@<VPS-IP>:/root/bookstack
ssh root@<VPS-IP>
bash /root/bookstack/bookstack.sh
```
Choose **Install & deploy → Quick install**. It runs System → Tailscale → Configure →
Cloudflare → Deploy → Audiobookshelf setup → Backups, sets the admin password, applies the
secure app defaults (registration off, Kobo sync on, convert-to-EPUB, per-user copies,
CWA's import-time Kindle fixer off), and then offers to add your first users. The token and
accounts it asks for are described in the section above. Afterwards: **Library → Mail** (SMTP
so users can Send-to-Kindle from the portal), **Security → Authelia** if you want SSO + 2FA,
**Operations → Self-test**. Every step is re-runnable from its submenu.

## The request flow
Users sign in at `https://request.<domain>` with their library credentials → search →
**Request**. By default (`APPROVALS_REQUIRED=true`) a non-admin request lands as **pending**;
the admin sees it in the portal's approval queue (and the `/admin` dashboard) and approves or
denies it. Admin requests skip the queue. On approval the book downloads centrally, is
tagged to the requester, and appears in *their* library within seconds (audiobooks go to
Audiobookshelf). From there it reaches the user's devices:
- **Kobo**: automatically at the next sync (they linked the device once on Devices).
- **Kindle**: automatically if they enabled auto-send, or with one click under My books.
- **Anything else**: Download under My books (preferred format first), OPDS, the ABS app.
Set `NOTIFY_WEBHOOK` to get a JSON POST on requested/approved/denied/done/error. Search
results flag titles already in the library. Library → Sources toggles approvals.

## Devices (what users do themselves, once)
On the portal's **Devices** page:
- **Kobo** — *Generate my Kobo link*, then on the device set
  `.kobo/Kobo/Kobo eReader.conf` → `[OneStoreServices] api_endpoint=<link>` and sync. The
  link is the same per-user token CWA generates; only their tagged books arrive. (Kobo/OPDS
  paths bypass Authelia automatically.) The admin can show/regenerate it from Users & devices.
- **Kindle** — enter their Send-to-Kindle address (stored in CWA's user record) and add the
  library's sender address to Amazon's approved list (the page shows which address). Then
  *Send to Kindle* under My books, or tick *auto-send* to receive every new ebook by mail.
- **Preferred format** — what *Download* hands out when several formats exist.
- **Notifications** — opt in to an e-mail when a request is ready or denied.
- **Account** — change their own password (portal, CWA, Shelfmark and Audiobookshelf at once).
- **Phone/tablet** — OPDS `https://books.<domain>/opds` in any reader; audiobooks via the
  Audiobookshelf app with the same login; KOReader progress sync at `/kosync` when enabled.

## Formats & conversion (Library → Formats)
Drives CWA's own settings in `cwa.db`, applied on the next import:
target format (EPUB recommended: Kobo receives KEPUB automatically on sync, Kindle accepts
EPUB by mail), convert on import on/off, Kindle EPUB fixer, originals to keep alongside, and
the duplicate policy (`new_record` required for isolation). Deploy applies the secure
defaults; users choose their own *download* format on Devices.

## Shelfmark — the CWA companion (Library → Shelfmark)
`https://shelf.<domain>` runs Shelfmark (`ghcr.io/calibrain/shelfmark`), the maintained
successor of *calibre-web-automated-book-downloader*:
- **Login = library login** (`AUTH_METHOD=cwa`, CWA's config mounted read-only at `/auth`).
  Shelfmark reads that database with SQLite's `immutable=1`, which ignores the write-ahead
  log, so the portal checkpoints the WAL after every user/device write — new accounts are
  visible to Shelfmark immediately. Shelfmark also only starts once CWA is healthy: with no
  `app.db` present it would run with *no* authentication.
- **Per-user destination**: `INGEST_DIR=/dropbox/{User}` → every ebook lands in
  `library/dropbox/<username>/`, is tagged `owner:<username>` and atomically ingested within
  ~15 s. File organization must stay `rename` (flat files).
- **Audiobooks** go straight to `library/audiobooks` as `Author/Title/`; tag `owner:<user>`
  in Audiobookshelf.
- **Sources are opt-in** in Shelfmark → Settings. For torrent-backed sources point its
  qBittorrent client at `http://qbittorrent:8080` (compose network, not localhost).
- Exposure identical to the portal (Cloudflare → mTLS → optional Authelia → its session);
  `/api/auth/*` shares the Caddy login rate limit. Health: `http://127.0.0.1:8084/api/health`.

## Ephemera — optional, Tailscale-only (Operations → Ephemera)
Adds a "request it and auto-download when it appears" queue and a newznab indexer mode.
**Status (Sept 2026):** the upstream repo `OrwellianEpilogue/ephemera` and its image were
removed from GitHub. The overlay *builds* the last release (v1.3.1, Nov 2025) from a
community re-upload pinned to commit `e98e9944…`; treat it as unmaintained. It is reachable
only at `https://ephemera.<domain>` over Tailscale behind the admin password, files
everything to one CWA user's dropbox (`EPHEMERA_OWNER`), and needs FlareSolverr (~0.5–1 GB
RAM). If you only need multi-user search + download, skip it; Shelfmark does that.

## Automated acquisition & distributed ingest (Library → Intake)
Four more ways files enter, all owner-mapped and run through the same state machine
(queued → downloading → importing → done) with atomic ingest and owner-tagging:
- **Per-user dropboxes** — `library/dropbox/<username>/` is watched (scp, rsync, Syncthing,
  WebDAV, rclone). Hidden/partial files are ignored until complete.
- **Browser upload** — the portal's Upload page.
- **Intake webhook** — `POST https://request.<domain>/intake` with `X-Intake-Token` and JSON
  `{"user":"alice","url":"https://.../book.epub"}`; pulls that exact URL for that user.
- **Email-to-library (optional IMAP)** — mail to `<mailbox>+alice@yourdomain`.
Gutenberg can pull from a local mirror; the OPDS source pulls from any catalog you host.

## Application hardening
Applied by Deploy / Users → Repair: CWA public registration OFF, anonymous browsing OFF,
Kobo sync ON, Kobo store proxy OFF, convert-to-EPUB, `new_record` duplicates, Kindle fixer.
Still yours to do once: **Audiobookshelf** root user + per-user tags; **qBittorrent** (dl.,
Tailscale) real Web UI password, "Bypass authentication for localhost", categories
`ebooks`,`audiobooks`,`owned-staging`, require encryption; **AriaNg** RPC secret; CWA SMTP
(Admin → Edit e-mail server) if you also want CWA's own Send-to-Kindle button; Uptime Kuma
monitors. Operations → Self-test checks the posture.

## Security & ops menu (what's where)
- **Install & deploy**: Quick install, System, Tailscale, Configure, Cloudflare, Deploy, Backups.
- **Users & devices**: list / add / Kindle / Kobo link / password / remove / repair / guide.
- **Library**: Formats & conversion, Mail (SMTP + test), Sources & approvals, Shelfmark,
  Intake & dropboxes, isolation guide.
- **Security**: Authelia enable / disable / add user, Lock SSH to Tailscale, fail2ban,
  SPF/DMARC, Cloudflare Access guide.
- **Operations**: Self-test, Status, Logs, Update, Backups, Restore test, Monitoring,
  Ephemera enable / disable.

Backups are encrypted restic (7 daily / 4 weekly / 6 monthly) — keep the repo password
safe. Update monthly. Watch `df -h /srv`; audiobooks fill 60 GB fastest.

## Authelia notes
Authelia gives login + TOTP/passkey 2FA + brute-force lockout in front of the public apps,
self-hosted. Enabling generates secrets, starts it, injects a Caddy `forward_auth` gate
(bypassing `/kobo/*` and `/opds`) and offers to create Authelia logins for existing users;
new users created afterwards get one automatically. **Test in a browser right after
enabling**; *disable* removes the gate instantly. Apps keep their own logins behind the gate
(defence in depth); double login is the trade for that.

## Testing (run before every deploy)
```
bash tests/run-unit.sh      # portal unit/integration tests inside the shipping image (~1 min)
bash tests/tui-test.sh      # installer logic: .env quoting, configure, gate, users, ABS, formats, mail
bash tests/stack-test.sh    # end-to-end with the REAL containers (~8-10 min); KEEP=1 to inspect
```
`run-unit.sh` needs Docker; it also runs `pip-audit` against the locked requirements, which
needs network (`SKIP_AUDIT=1` for an offline run). Lint the Python with an isolated ruff:
`uvx ruff check --select E9,F63,F7,F82,F401,F811,F841,E711,E712 librarian`.
On the server: **Operations → Self-test** (containers, endpoints, Caddy/Authelia config,
firewall, loopback binds, isolation in CWA and ABS, public reachability, backups).
The unit tests use schemas dumped from the real CWA/Calibre databases
(`librarian/tests/fixtures/`) and a scripted double of the ABS API. The end-to-end run
brings up CWA, Audiobookshelf, Shelfmark and the portal plus test doubles (a real
SMTP/IMAP server, a file server, Authelia behind a Caddy that uses the production
forward_auth snippet) and walks ~150 checks: users created by the installer's CLI are
accepted by CWA, Shelfmark and ABS; a simulated Kobo device receives only the owner's books;
uploads, intake-webhook pulls, Shelfmark-style drops and e-mailed attachments all end up
tagged and imported; Send-to-Kindle and auto-Kindle mail is captured; lockout, quota, CSP,
audit trail and notifications behave; the Authelia gate redirects browsers and lets
`/kobo/*` and `/opds` through; audiobooks are tagged to their owner automatically. It also
prints peak memory per container (see the VPS section) and fails on any worker crash.

## Files
- `bookstack.sh` — the menu (installer + manager, re-runnable)
- `docker-compose.yml` (+ `.authelia.yml`, `.ephemera.yml` overlays) — services (hardened)
- `librarian/` — the portal (Flask): routes, worker, adapters, `cwa.py` (user/device writes),
  `library.py` (tag-scoped reads), `kindle.py` (SMTP), templates, tests
- `caddy/` — Caddy build + hardened template (mTLS, headers, rate limits, gate marker)
- `authelia/` — optional SSO config (rendered on enable)
- `scripts/` — Cloudflare-IP firewall sync, encrypted backup, restore test, disk watchdog,
  alerting, **selftest**
- `configs/fail2ban/` — jails + Caddy filters (login and device-auth paths)
- `tests/` — unit runner, installer harness, end-to-end UAT + driver
- `docs/` — deployment checklist, research sweep (`RESEARCH-GAPS.md`), pending decisions

## VPS sizing (measured)
Peak resident memory per container during the full end-to-end run (laptop, arm64, five runs):

| Container | Peak |
|---|---|
| calibre-web (CWA, incl. an import + Kindle fixer) | 450–575 MiB |
| authelia | ~240 MiB |
| audiobookshelf | 150–200 MiB |
| shelfmark | 140–195 MiB |
| portal (librarian) | ~70 MiB |
| caddy | ~55 MiB |
| qbittorrent / aria2 / AriaNg / Uptime Kuma (not in the run) | ~150 / 30 / 10 / 150 MiB typical |

Core stack ≈ 1.3–1.7 GB resident, plus calibre conversion spikes (a few hundred MB per
`ebook-convert`), Audiobookshelf's first scan (up to 1–1.5 GB on a big library) and the OS.

**Recommendation: the 4-core / 8 GB / 160 GB plan (X8).** Disk is the binding constraint:
on 80 GB, after OS, images, swap and ebooks, roughly 50 GB remains for audiobooks (100–150
titles). The 2-core / 4 GB / 80 GB plan (X4) is defensible for ≤ ~10 users if audiobooks stay
under ~40 GB, Ephemera stays off and Shelfmark's browser-backed sources stay off; the memory
fences in `docker-compose.yml` keep one runaway container from taking the box down, and the
installer adds swap. Upgrade signals and the full working are in `docs/RESEARCH-GAPS.md`
section 2. The 1-core / 2 GB plan is not viable. Bandwidth (10 TB) is a non-issue.

## Known limits
- Isolation is visibility, not separate storage — admin sees all files.
- Cloudflare terminates TLS at its edge (price of the DDoS layer).
- Send-to-Kindle routes through Amazon; Kobo/OPDS talk only to your server.
- Authelia password resets are separate from library passwords (re-add the user in
  Security → Authelia after a library password reset).
- Ephemera is unmaintained upstream; only Shelfmark is the long-term companion.
