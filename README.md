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

## What changed in v4.3 (fan-out audit + synthetic user journeys)
Five auditors, each checked by a skeptic, went through the installer, the service wiring, the
portal, every family journey and operations; every verified finding (8 high, 24 medium, 37
low, 8 simplifications) is fixed and covered by tests. Highlights:
- **Alerts reach you.** A required Alerts step (ntfy or webhook, with a test); the scripts
  fall back to a direct post when the portal is down; the self-test fails with no channel.
- **Nothing on the admin side can take the public sites down.** Admin sites no longer bind the
  Tailscale address (they listen normally and drop anyone outside the tailnet); the nightly
  Cloudflare-IP refresh never touches the firewall when a download fails.
- **Real client IPs** from `CF-Connecting-IP` for every rate limit, lockout and log.
- **Dropboxes behave:** re-dropped files import, folders are classified properly, oversized
  files are parked, restarts cannot loop, audiobook tags are retried until confirmed.
- **Restore and update you can trust:** snapshot picker, config-only restore, free-space check,
  in-place restore; Update ships the new code, gates on health and rolls back images.
- **SSH and firewall stay locked** across re-runs; the SSH drop-in wins over cloud-init.
- **Smaller surface for a family:** aria2/AriaNg removed, torrents opt-in and owner-tagged,
  approvals and the intake webhook off by default, a chosen admin username instead of
  `admin`, a DNS-only token for Caddy, scripts owned by root.
- **BookOrbit** was studied and trialled next to a copy of the library: it can run read-only
  alongside CWA, but for now it is recommended as an admin-only trial (see
  `docs/DECISIONS-PENDING.md`).

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
(audio), plus **your own OPDS catalog**. Standard Ebooks is off by default (its feed now
needs a Patrons Circle login). The portal's source set is fixed in code — it is not a
general indexer.

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
| monitor .mfdata.in | You, from your other Tailscale devices | Caddy aborts any client outside the tailnet → app login |
| dl .mfdata.in (only with Torrents on) | You, from your other Tailscale devices | tailnet-only → password gate → qBittorrent login |
| ephemera.mfdata.in (optional) | You, from your other Tailscale devices | tailnet-only → password gate (Ephemera has no login of its own) |
| SSH | You, only on Tailscale (after locking) | Key-only |
| Port 6881 (only with Torrents on) | Torrent peers | qB peer port only — no UI, no files |

Origin locked three ways (IP not in public DNS as an origin, firewall accepts 80/443 only
from Cloudflare, Caddy requires Cloudflare's client cert). Caddy takes the visitor's address
from Cloudflare's `CF-Connecting-IP` only, so rate limits, lockouts, fail2ban and the audit
trail see the real client and cannot be fooled by a forged `X-Forwarded-For`. Every container binds 127.0.0.1
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
| DNS | Edit | create the `books`, `audio`, `request`, `shelf`, `auth`, `monitor` (and `dl` with torrents) records |
| Zone Settings | Edit | SSL Full (strict), TLS 1.2+, HTTPS forced, Authenticated Origin Pulls, e-reader-breaking features off |
| Config Rules | Edit | per-host exceptions for the device paths |
| Cache Rules | Edit | never cache a user's book or audio response |
| Firewall Services | Edit | fail2ban bans abusive visitors at Cloudflare |

Under **Zone Resources** pick *Include → Specific zone → your domain*. Leave client IP
filtering empty (the server's IP changes if you ever rebuild) and set no expiry, or put a
reminder in your calendar. Copy the token once; Cloudflare will not show it again.

Optionally create a **second token with only Zone → DNS → Edit** for the same zone. Configure
asks for it: Caddy, the one internet-facing process, then only holds that narrow token for
certificate renewals, and the powerful one stays on the host. Leave it blank to reuse the
main token.
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

**3. An alert channel (required).** Failed backups, a full disk or a failed restore test must
reach you. **Quick install → Alerts** asks for an [ntfy](https://ntfy.sh) topic URL (free: pick
a long random topic name and subscribe to it in the ntfy phone app) or any webhook, and sends
a test. For a dead-man check on backups, optionally add a free healthchecks.io URL in the
Backups step.

**4. Outgoing mail (optional, for Send-to-Kindle and notifications).** An SMTP account the
server sends from, set once by you in **Library → Mail**. Any provider that gives SMTP
credentials works (a Gmail or Fastmail app password, Brevo, Mailgun, your domain's mail host).
Users do not configure mail. Each user does two things themselves: enter their Kindle
address on the portal's **Devices** page, and add the server's *From* address (shown on that
page) to **Amazon → Manage Your Content and Devices → Preferences → Personal Document
Settings → Approved Personal Document E-mail List**. The **Send a test to my Kindle** button
proves it works.

**5. A backup repository off the server (recommended).** Any restic repository: an S3
bucket (Backblaze B2, Wasabi, Cloudflare R2) or an SFTP host. **Quick install → Backups**
asks for the repository URL, a password and the access keys. Keep the password somewhere
other than the server; without it the backups cannot be restored.

**6. Shelfmark release sources.** Nothing is enabled by default. After deploy, open
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
**Request**. Approvals are **off** by default for a family (`APPROVALS_REQUIRED=false`; they
only ever covered the public-domain catalogs, not Shelfmark or uploads). Turned on in
Library → Sources, a non-admin request lands as **pending** in the portal's approval queue
until the admin approves or denies it. The book then downloads centrally, is
tagged to the requester, and appears in *their* library within seconds (audiobooks go to
Audiobookshelf). From there it reaches the user's devices:
- **Kobo**: automatically at the next sync (they linked the device once on Devices).
- **Kindle**: automatically if they enabled auto-send, or with one click under My books.
- **Anything else**: Download under My books (preferred format first), OPDS, the ABS app.
Audiobooks show the status **tagging** until Audiobookshelf has confirmed the owner tag; one
still untagged after 24 h becomes *needs-tag* and the admin is alerted. Search results flag
titles already in the requester's library.

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
  Audiobookshelf app with the same login; KOReader progress sync via the plugin at
  `https://books.<domain>/kosync` when enabled.
- **E-mail** — their own address (for notifications and mail-in).

## Formats & conversion (Library → Formats)
Drives CWA's own settings in `cwa.db`, applied on the next import:
the target format is always EPUB (Kobo receives KEPUB automatically on sync and users can
download it as `.kepub.epub`; Kindle accepts EPUB by mail), convert on import on/off, CWA's
import-time Kindle fixer (keep off), originals to keep alongside, and
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
  ~15 s. Limit its formats to epub, pdf and cbz.
- **Audiobooks** land in the same dropbox; an audio-only folder becomes one audiobook, tagged
  to its owner in Audiobookshelf automatically. A folder of ebooks is imported book by book;
  mixed or empty folders are parked in `.failed` with a reason.
- **Sources are opt-in** in Shelfmark → Settings.
- Shelfmark refuses to start without CWA's `app.db` and reports unhealthy if its auth mode is
  ever anything but `cwa`.
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
- **Intake webhook** (off until enabled in Library → Intake; `/intake` answers 404 until
  then) — `POST https://request.<domain>/intake` with `X-Intake-Token` and JSON
  `{"user":"alice","url":"https://.../book.epub"}`; pulls that exact URL for that user.
- **Email-to-library (optional IMAP)** — mail to `<mailbox>+alice@yourdomain`. Filed only
  when the receiving server's `Authentication-Results` shows DMARC, DKIM or SPF passing for the
  sender (`IMAP_REQUIRE_AUTH=false` for a local relay).
A re-dropped file with the name of an earlier failure is imported; a second upload with the
same name becomes `name (2).ext` instead of overwriting. Size caps: 500 MB ebooks, 2 GB audio,
PDFs over 250 MB are not tagged (parked for users).
Gutenberg can pull from a local mirror; the OPDS source pulls from any catalog you host.

## Application hardening
Applied by Deploy / Users → Repair: CWA public registration OFF, anonymous browsing OFF,
Kobo sync ON, Kobo store proxy OFF, convert-to-EPUB, `new_record` duplicates, CWA's Kindle
fixer OFF, duplicate auto-resolve and metadata tag updates OFF.

**Torrents are opt-in** (Library → Torrents). Enabling starts qBittorrent, opens port 6881,
creates `dl.<domain>` and seeds its default save path to your dropbox; point every category at
`/dropbox/<user>` so downloads are owner-tagged like everything else. aria2 and AriaNg were
removed.

Still yours to do once: a real qBittorrent Web UI password (if you enable torrents), CWA SMTP
(Admin → Edit e-mail server) if you also want CWA's own Send-to-Kindle button, and Uptime Kuma
monitors (loopback checks such as `http://127.0.0.1:8084/api/auth/check`, keyword `cwa`, now
work) plus one free external monitor for "the whole VPS is down". Operations → Self-test
checks the posture.

## Security & ops menu (what's where)
- **Install & deploy**: Quick install, System, Tailscale, Configure, Cloudflare, Deploy, Backups, Alerts.
- **Users & devices**: list / add / Kindle / Kobo link / password / remove / repair / guide.
- **Library**: Formats & conversion, Mail (SMTP + test), Sources & approvals, Shelfmark,
  Intake & dropboxes (webhook enable/show/disable), Torrents, isolation guide.
- **Security**: Authelia enable / disable / add user, Lock SSH to Tailscale, Reopen public SSH,
  fail2ban, Bans — list and release, Clear a login lockout, SPF/DMARC, Cloudflare Access guide.
- **Operations**: Self-test, Status, Logs, Restart a service, Update, Backups, Restore test,
  Restore from backup, Restore a single file, Alerts, Monitoring, Ephemera enable / disable.

Backups are encrypted restic (7 daily / 4 weekly / 6 monthly by default — `RESTIC_KEEP_DAILY`
/ `_WEEKLY` / `_MONTHLY` in `.env`; `pre-update` snapshots kept 90 days; `restic check` weekly;
a monthly restore test on the 1st) — keep the repo password safe. **Restore** lets you pick a snapshot and restore everything or only config +
databases; it checks free space first and restores in place. **Update** deploys the code from
the checkout it runs from, gates on container health, and can roll back to the previous
images and Caddyfile. Watch `df -h /srv`; audiobooks fill the disk fastest.

## When things go wrong
Everything here is a menu entry in `bookstack.sh` over Tailscale SSH. That is the admin
console; the web `/admin` page is a read-mostly dashboard and says so. Start with
**Operations → Self-test**: it names the menu entry for almost every failure it reports.

**"Convert library" / "EPUB fixer" / "Show logs" in Calibre-Web's own admin page do nothing
(403).** That is deliberate and it also hits *you*. Calibre-Web Automated v4.0.6 registers
those four blueprints (`/cwa-convert-library*`, `/cwa-epub-fixer*`, `/cwa-logs*`,
`/cwa-internal/*`, plus `/reconnect`) with **no authentication at all** — one anonymous GET,
or a cross-site `<img src>` loaded in your logged-in browser, starts a library-wide
conversion or runs the EPUB fixer, which rewrites every EPUB and drops the `owner:` tags this
stack's per-user isolation depends on. Caddy therefore returns 403 on those paths for
everyone, gate or no gate, so the buttons on CWA's admin page fail for the admin too. They
are not needed in normal use (ingest converts on import — Library → Formats). If you really
want one, run it from the server itself, where the block does not apply:

```sh
ssh root@<tailscale-ip>
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8083/cwa-convert-library-start
docker logs -n 100 calibre-web      # watch it
```

**A family member (or you) is suddenly locked out of every site.** fail2ban bans at
Cloudflare, and the household shares one public address, so one ban cuts off books., audio.,
request. and shelf. at once for everybody. Use **Security → Bans — list and release**: it
shows the banned addresses per jail and releases them *both* locally and at Cloudflare (the
local `fail2ban-client unbanip` alone does not remove the Cloudflare IP Access Rule).
Audiobookshelf apps holding a stale password are the usual cause — fix the saved password in
the app, or the ban comes straight back.

**"Too many attempts, try again later" on the portal.** That is the portal's own lockout
(`LOCKOUT_*` in `.env`), separate from fail2ban. **Security → Clear a login lockout** releases
a user or an address immediately; it expires by itself after `LOCKOUT_SECONDS`.

**A service is wedged.** **Operations → Restart a service**. It recreates the container
(`up -d`, not `restart`), so a setting the TUI just wrote into `.env` is picked up too.

**You ran Security → Lock SSH and Tailscale is unavailable.** Reach the VPS through your
provider's serial/web console, log in as root and run `bash /srv/bookstack/bookstack.sh` →
**Security → Reopen public SSH**. Key-only authentication stays enforced.

**One file needs to come back, not the whole stack.** **Operations → Restore a single file
from backup** — pick a snapshot and a path; it restores beside the live copy so nothing is
overwritten until you say so. **Operations → Restore from backup** is the whole-stack path.

**Downloads and imports stopped on their own.** The disk watchdog stops Shelfmark and
qBittorrent at `DISK_STOP_PCT` (95 % by default) and raises
`library/staging/.disk-paused`, which also pauses the portal's own queue, dropbox watcher and
mail intake — otherwise they keep writing 2 GB files onto a full disk. Free space; below
`DISK_RESUME_PCT` (80 %) the watchdog starts everything again and clears the flag. If a
service does not come back it says so and keeps retrying hourly rather than claiming success.

**Backups.** Self-test asserts that a snapshot actually exists and is under 36 h old, not just
that the timer is installed — an installed timer whose every run fails looks identical
otherwise. On a failure: `journalctl -u bookstack-backup -n 50`, then
`bash /srv/bookstack/scripts/backup.sh` by hand to read the error directly.

## Authelia notes
Authelia gives login + TOTP/passkey 2FA + brute-force lockout in front of the public apps,
self-hosted. Enabling generates secrets, starts it, injects a Caddy `forward_auth` gate and
offers to create Authelia logins for existing users;
new users created afterwards get one automatically. **Test in a browser right after
enabling**; *disable* removes the gate instantly. Apps keep their own logins behind the gate
(defence in depth); double login is the trade for that.

Paths that cannot do SSO are bypassed: `/kobo/*`, `/opds`, `/kosync` on books.; the
Audiobookshelf apps' own login, token refresh, API, sockets, streams and feeds on audio.,
plus `POST /init` so a **fresh** Audiobookshelf can still have its first root user created
with the gate on; the intake webhook on request. The bypass list lives in two files that must
stay equivalent — `authelia/inject-gate.py` (what Caddy forwards) and
`authelia/configuration.yml.template` (what Authelia allows). The looser of the two is the one
that decides, so keep them anchored the same way; `tests/e2e_driver.py` asserts a few of the
edges (`/opdsfoo` is *not* bypassed, bare `/socket.io` *is*).

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
| qbittorrent (opt-in) / Uptime Kuma (not in the run) | ~150 / 150 MiB typical |

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
