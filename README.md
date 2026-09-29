# mfdata.in private library — hardened, per-user, self-serve (v6)

A private book/audiobook library on one small VPS. **Admins** install and run everything from
one menu (`bookstack.sh`). **Users** only ever see the portal: they sign in, search, request
or upload a book, and it appears on their Kobo, their Kindle, their phone, or as a direct
download — only their own books, never anyone else's. Nothing is public.

## What you get

| For end users (`request.<domain>`) | For the admin (`bookstack.sh`, plus `/admin` in the portal) |
|---|---|
| **Search for any book** (metadata-first), open its page, **Request** a verified copy or **Keep looking** | **Quick install**: system → Tailscale → Cloudflare → deploy → backups, in order |
| **Upload** a book they own | **Users & devices**: create an isolated account, Kobo link, Kindle address, passwords — one screen |
| **My books**: download in their preferred format, or **Send to Kindle** | **Library**: target format & conversion policy, SMTP for Send-to-Kindle, sources, Shelfmark, intake |
| **Devices**: generate their Kobo sync link, set their Kindle address, pick a format, opt into auto-send | **Security**: Authelia SSO + 2FA gate, SSH lock, fail2ban, SPF/DMARC |
| Extended search at `shelf.<domain>` (Shelfmark) with the same login | **Operations**: self-test, status, logs, update, restore test, Ephemera |

Everything an admin previously had to click through in three different web UIs (user
creation, Allowed Tags, Kobo sync toggle, registration off, conversion settings, Kindle
addresses) is now done by the menu or by users themselves in the portal.

## What changed in v5 (metadata-first search, fetch and enrichment)
Designed from how Readarr, Shelfmark, CWA and Ephemera actually do it (their source was read,
not guessed — see `docs/PLAN-v5.md`), then built and proven on the real containers.
- **Search finds the BOOK first** (Open Library, keyless): covers, authors, years, editions,
  "free ebook / free audiobook" and "in library" badges. **Author pages** for any author (bio,
  photo, works) and **series** listings from the Goodreads mirror.
- **Each book's page lists copies we can actually fetch**, resolved through the book's own
  catalogue links (Open Library records the same work's Gutenberg, LibriVox, Standard Ebooks and
  Internet Archive ids) plus a Readarr-style keyword search of every enabled catalogue. Every
  copy is **checked against the book** — title, author, ISBN, language, abridged/adapted,
  omnibus — with a Readarr-style weighted distance, and shows why ("good match" / "check this
  one" / refused with the reason). A copy in another language than the reader's is never
  fetched for them (each reader sets their language on Devices).
- **Request buttons post a token, never an address**: the download carries the source's own
  evidence (Internet Archive's size and SHA-1 are now actually checked — the old form dropped
  them), the work it was chosen as, and the match reasons.
- **Keep looking**: a book no catalogue has yet is re-checked on a widening schedule (1 h, 6 h,
  daily, for 180 days) through its catalogue links and the catalogues; an exact match is
  requested automatically, an uncertain one waits for the reader's yes.
- **Your own catalogs**: any number of OPDS feeds (Calibre, Calibre-Web, COPS, Kavita, Komga,
  BookLore, a library's feed), added from the admin page or the TUI, searched everywhere.
- **Shelfmark's own metadata search works** (it shipped with no provider switched on and
  answered "No metadata provider configured"); Hardcover and Google Books keys are optional
  (Library → Metadata sources). **Shelfmark obeys the portal's approval rule**, and its pending
  downloads are approved on the portal's Pending card — one queue.
- **Enrichment** looks books fetched from a book page up exactly (by Open Library work), adds
  descriptions, and the host job now fills a missing **cover, description, publisher, date,
  language and ISBN** into Calibre so the Kobo shows them — fill-only, never overwriting.
- **Convert to any format** from a book's page (EPUB, AZW3, MOBI, PDF, TXT, DOCX, FB2, RTF) with
  Calibre's own converter, and **real KEPUB downloads** for Kobo readers.
- **MOBI/AZW3/FB2/TXT get their owner tag** added in Calibre by the host job (L10) within a
  few minutes, also after CWA converted them to EPUB (the file's own title and author are read
  at arrival to find the book again); the admin hears only if that fails. **Send-to-Kindle
  runs in the background** (no more 524 on a slow mail relay).
- **Family sharing.** A book already in the library is given to the next reader who asks,
  never downloaded or imported twice (seedbox traffic, tracker ratio, disk): their owner tag is
  added to the same copy. It applies to Shelfmark requests (before anything downloads), to
  dropbox / Shelfmark arrivals and to portal requests, for ebooks and audiobooks. Only a strong
  match counts: ISBN, Calibre UUID, or the same title AND author. `FAMILY_SHARING=false` in
  `.env` turns it off.
- **One login behind the gate** (Authelia on): the portal and Calibre-Web trust Authelia's
  answer, so a family member signs in once. Caddy strips `Remote-User` on every path and adds a
  secret only to requests Authelia let through; a portal password change reaches Authelia's
  own user file within seconds (host job, a hash, never the password). Shelfmark keeps its own
  login, with the same password.
- **Containers can no longer talk to each other**: each service has its own network (Shelfmark
  shares one with FlareSolverr only), and capabilities are dropped.
- **The origin can be locked to your zone**: Security → Origin lock swaps Cloudflare's shared
  client certificate for one this server issues and uploads (proven with a request through
  Cloudflare before the old one is dropped; renewal and expiry warnings included).
- **Backups the server cannot delete**: with an append-only B2 key the nightly job never
  prunes; retention runs monthly with a separate key (on the server or from your own
  computer), and a snapshot that vanishes is an alert the next night.
- **A canary reader**: twice a day two hidden test accounts upload, import, download, check
  isolation, OPDS, Kobo and Shelfmark; failures alert with the failing step, and /admin shows
  the import time (it climbs before Calibre-Web fails).
- **qBittorrent gets a real password** before its first start (no temporary one to fish out of
  the logs).
- Found on the way and fixed: calibredb (as the library user) printed a warning on stdout that
  broke every JSON read of the host metadata push on the real image; Standard Ebooks serves an
  HTML page at its plain download address; finished imports were never joined to their Calibre
  book, so the metadata push could not reach them.

## What changed in v4.3 (fan-out audit + simulated user journeys)
Five auditors, each checked by a skeptic, went through the installer, the service wiring, the
portal, every family journey and operations; every verified finding (8 high, 24 medium, 37
low, 8 simplifications) is fixed and covered by tests.

To be exact about the journeys, because the wording used to invite the wrong conclusion: they
were run once, locally, during development, against a throwaway stack. They are **not** a
scheduled canary on your server — nothing replays a user journey there. What DOES run on the
server unattended is the post-reboot self-test (below). A recurring canary is a deliberate
deferral, recorded in `docs/RESEARCH-GAPS.md`. Highlights:
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
  and are excluded from auto-conversion; MOBI/AZW3/FB2/TXT get it in Calibre after the import
  (host job) and reach the admin as `needs-tag` only if that fails; unsupported files and any symlink planted in a dropbox are parked under
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
| home.mfdata.in (v6.0 start page: /hub, /help, /static only; every other path redirects to request.) | Your users, anywhere | Cloudflare → mTLS-locked origin → **(optional) Authelia sign-in** → portal login |
| request / books / audio / shelf .mfdata.in | Your users, anywhere | Cloudflare DDoS+WAF+bot → mTLS-locked origin → **(optional) Authelia SSO (admins: 2FA always; readers: 2FA with AUTHELIA_READERS_2FA)** → per-user app login |
| auth.mfdata.in | Users (only when Authelia on) | The SSO portal itself |
| monitor .mfdata.in | You, from your other Tailscale devices | Caddy aborts any client outside the tailnet → app login |
| dl .mfdata.in (only with Torrents on) | You, from your other Tailscale devices | tailnet-only → password gate → qBittorrent login |
| ephemera.mfdata.in (optional) | You, from your other Tailscale devices | tailnet-only → password gate (Ephemera has no login of its own) |
| SSH | You, only on Tailscale (after locking) | Key-only |
| Port 6881 (only with Torrents on) | Torrent peers | qB peer port only — no UI, no files |

Origin locked three ways (IP not in public DNS as an origin, firewall accepts 443 only
from Cloudflare, Caddy requires a Cloudflare client certificate). What that certificate proves
depends on which one: Cloudflare's **shared** origin-pull certificate is presented for every
Cloudflare customer, so it proves "a Cloudflare edge", not "your zone" (an attacker would still
need their own Cloudflare zone pointing at your IP, and Caddy's per-hostname certificates make
that harder). **Security → Origin lock** replaces it with a certificate this server issues and
uploads to your zone (needs the token permission SSL and Certificates → Edit); then only
requests through *your* zone pass the handshake. The strongest option, Cloudflare Tunnel (no
public web ports at all), is not automated. Caddy takes the visitor's address
from Cloudflare's `CF-Connecting-IP` only, so rate limits, lockouts, fail2ban and the audit
trail see the real client and cannot be fooled by a forged `X-Forwarded-For`. Every container binds 127.0.0.1
with `no-new-privileges`. No anonymous browsing, no self-registration, no third-party
scripts, no telemetry. In the portal: CSRF tokens on every form, HttpOnly/Secure/Lax
sessions that expire after 12 h, a strict Content-Security-Policy (script-src 'self': the portal's one
first-party script, static/app.js, never inline or third-party code),
brute-force lockout per user+IP and per IP, a daily request quota, an audit trail, and
tag-scoped path-confined downloads. At Caddy: login, download and intake rate limits per
real client IP; optionally fail2ban bans repeat offenders at Cloudflare. Authelia adds
SSO in front of everything but the device endpoints: a second factor (TOTP, passkey, security
key) for admins always, for readers when AUTHELIA_READERS_2FA=true.

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
| Cache Rules | Edit | never cache a user's book or audio response |
| Firewall Services | Edit | fail2ban bans abusive visitors at Cloudflare |
| SSL and Certificates | Edit | optional: this zone's own origin-pull certificate (Security → Origin lock) |

Under **Zone Resources** pick *Include → Specific zone → your domain*. Leave client IP
filtering empty (the server's IP changes if you ever rebuild) and set no expiry, or put a
reminder in your calendar. Copy the token once; Cloudflare will not show it again.

Optionally create a **second token with only Zone → DNS → Edit and Zone → Zone → Read** for the same zone (Caddy's Cloudflare plugin looks the zone up by name). Configure
asks for it: Caddy, the one internet-facing process, then only holds that narrow token for
certificate renewals, and the powerful one stays on the host. Leave it blank to reuse the
main token.
Paste it when **Quick install → Configure** asks for it. It is stored only in
`/srv/bookstack/.env` (root-only, mode 600) and read by Caddy at runtime. To replace it later,
re-run **Install & deploy → Configure** and paste the new one.

On a paid Cloudflare plan, turn **Security → WAF → Managed rules ON** (on the Free plan the managed rules are an upgrade; Cloudflare's free baseline protection runs by itself, nothing to buy). Leave **Bot Fight
Mode OFF**: it challenges Kobo, OPDS, KOReader and the Audiobookshelf apps, cannot be
exempted on the Free plan, and the devices fail silently.

**Browser Integrity Check is turned off for the whole zone, not per path.** The device paths
(`/opds`, `/kosync`, `/kobo/<token>`, the Audiobookshelf apps) are not browsers and fail the
check silently, and the Free plan has no per-path exception that works for them — so the
installer sets `browser_check: off` zone-wide and the HTML apps lose that layer too. The
accepted trade: the WAF managed rules, Caddy's per-path rate-limit zones and the fail2ban
jails (which ban at Cloudflare) are the defence for the HTML apps instead. No Configuration
Rule is created, which is why the token above needs no Config Rules permission — an earlier
version of this table asked for one for a feature that was never built.

**2. Tailscale.** Create an account and install the app on the devices *you* administer
from. The free Personal plan is enough: only you and the server join the tailnet. End users
never need Tailscale; they use the public sites through Cloudflare. **Quick install →
Tailscale** prints a login link for the server.

**3. An alert channel (required).** Failed backups, a full disk or a failed restore test must
reach you. **Quick install → Alerts** asks for an [ntfy](https://ntfy.sh) topic URL (free: pick
a long random topic name and subscribe to it in the ntfy phone app) or any webhook, and sends
a test. For a dead-man check on backups, optionally add a free healthchecks.io URL in the
Backups step.

This channel is **yours alone**: readers never see it (they get e-mail, if they opt in on
Devices). It carries every reader's activity and every server problem:

| What | Priority | Tapping it opens |
|---|---|---|
| A request waiting for your approval (portal or Shelfmark), with a **Review** button | high | the Pending card |
| A failed request, a failed Shelfmark download, a book that needs an owner tag | high / default | /admin |
| Requested, approved, added, given from the family library, Keep looking results | low (silent) | /admin |
| Server problems (disk, self-test, seedbox, certificates, restarts) and their all-clear | by severity | /admin |
| **Daily disk summary** (09:05; `DISK_REPORT_HOUR`, `DISK_REPORT=false` in Advanced) | low; default at `DISK_WARN_PCT` | /admin |

On ntfy each one has an emoji for its kind, and **one notification per request or problem**:
a request's later steps (approved, added) and a problem's all-clear ("Disk back to 70 %",
"self-test passes again", "seedbox connected again") *replace* the earlier notification
instead of adding another, and today's disk summary replaces yesterday's.

**4. Outgoing mail (optional, for Send-to-Kindle and notifications).** An SMTP account the
server sends from, set once by you in **Library → Mail**. Any provider that gives SMTP
credentials works (a Gmail or Fastmail app password, Brevo, Mailgun, your domain's mail host).
Users do not configure mail. Each user does two things themselves: enter their Kindle
address on the portal's **Devices** page, and add the server's *From* address (shown on that
page) to **Amazon → Manage Your Content and Devices → Preferences → Personal Document
Settings → Approved Personal Document E-mail List**. The **Send a test to my Kindle** button
proves it works.

**5. A backup repository off the server (recommended).** **Quick install → Backups** offers three
places:
- **A computer at home, over Tailscale — free, no subscription.** It runs restic's own
  `rest-server` in Docker with `--append-only`, so this server can add backups but never delete
  one (measured: a delete from the server is refused with 403). The step prints everything to
  do on that computer: one `.htpasswd` line, one `docker run`, one Tailscale rule letting this
  server reach that single port, and the monthly command that clears out old backups there. It
  checks the login from here before storing anything. The computer has to be on at 01:00.
- **A storage bucket** (Backblaze B2, Wasabi, Cloudflare R2). Paid past their free tiers; B2 can
  be made append-only with a key that lacks `deleteFiles`.
- **Other**: an SFTP host or a local path.

Keep the backup password somewhere other than the server; without it the backups cannot be
restored.

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

## Book information (metadata)

Every book gets a page — cover, author, series and position, publication details and a
description — and **My books** links to it. Authors and series have pages too; a series page
tells a reader which numbers they are missing and which comes next. What it is, plainly:

- **Where it comes from.** A chain of providers, asked in the background a few minutes after a
  book imports, never while someone is waiting on a page: bookinfo.pro (the Goodreads mirror
  that replaced Readarr's metadata service), then its Hardcover twin, then Open Library, which
  needs no key and is the floor. A provider that fails three times in a row is stood down for
  15 minutes; a provider that simply has no answer about one book is NOT counted as failing.
  A book the whole chain has never heard of is not re-asked for a week.
- **Where it lives.** In the portal's own database. Your stored book files are never modified
  for it. The one thing written into a file remains the `owner:` tag, and that is access
  control, not description.
- **How the devices get it.** Kobo shows what Calibre's database says, so every 2 minutes a
  host job (`scripts/metadata-push.sh`) writes title, title sort, authors and series into
  Calibre — but only to FILL GAPS: a title a family member corrected by hand, or a series they
  set themselves, is never overwritten. It reads each book's owner tag before and after the
  write and raises a high-priority alert if it ever changed. Kindle reads the title from the
  file, so the copy mailed to a Kindle carries the library's title and author; the stored file
  is not touched.
- **What it is for besides looks.** Identification. A download from Shelfmark, the dropbox or
  e-mail is identified by what the FILE says (its title, author and ISBN) instead of its
  filename; the "in library" badge on search matches ISBN, then title with author, so a
  subtitle no longer hides a book you have and a same-titled book by someone else no longer
  claims you have it; and a download from the Internet Archive is checked against the size and
  SHA-1 the Archive publishes before it is imported — a mismatch waits for you in
  **needs-review** rather than being imported or silently thrown away.
- **Privacy between readers.** Author and series pages are only reachable from a book you own
  and only list your own books. Gaps in a series are worked out from YOUR copies, never from
  what a sibling has — the metadata store is shared across the household, and it would
  otherwise say what everyone else is reading.
- **Search and fetch (v5).** Searching now finds the BOOK first (Open Library), and a book's page
  lists the copies the stack can fetch, each checked against the book (see "What changed in v5").
  The portal fetches from the free catalogues and your own OPDS catalogs; everything else comes
  through Shelfmark, which the book page links to with the book's title, author and ISBN filled in.
- **Covers and descriptions on the devices (v5).** The host job also fills a missing cover,
  description, publisher, publication date, language and ISBN into Calibre — each only when
  Calibre has none. A cover is fetched only from the providers' image hosts, at most 8 MB, and
  only when the bytes really are an image.

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
the target format is always EPUB (Kindle accepts EPUB by mail), convert on import on/off,
CWA's import-time Kindle fixer (keep off), originals to keep alongside, and
the duplicate policy (`new_record` required for isolation). Deploy applies the secure
defaults; users choose their own *download* format on Devices.

**Kobo reading services (CWA v4.0.7+).** v4.0.7 points a Kobo's reading-services calls at this
site and answers four of them itself (`/api/v3/content/checkforchanges`, `…/annotations`,
`/api/UserStorage/…`, `/api/internal/notebooks`) with empty JSON; a Kobo whose calls fail
aborts its whole sync. Caddy lets exactly those four shapes through (and Authelia bypasses
them); every other path under `/api/v3`, `/api/UserStorage` and `/api/internal` is still the
open relay to Kobo's servers and stays 403. With an older CWA pinned (a rollback), Deploy
renders the full block again, because there those same paths ARE the relay.

**Kobo receives EPUB, not KEPUB.** CWA v4.0.6 and v4.0.7 look for `kepubify` only under
`/opt/kepubify/` while its image installs it at `/usr/bin/kepubify`, so KEPUB conversion is
never enabled and never has been here. EPUB syncs and reads fine on a Kobo; the one
difference is that reading position is recorded at chapter boundaries rather than
continuously. Turning KEPUB on is not a one-line change — doing it the obvious way makes Kobo
sync fail with a permanent HTTP 500 while every health check stays green. The measurement and
the safe three-step path are in `docs/DECISIONS-PENDING.md`.

## Shelfmark — the CWA companion (Library → Shelfmark)
`https://shelf.<domain>` runs Shelfmark (`ghcr.io/calibrain/shelfmark`), the maintained
successor of *calibre-web-automated-book-downloader*:
- **Login = library login** (`AUTH_METHOD=cwa`, CWA's config mounted read-only at `/auth`).
  Shelfmark reads that database with SQLite's `immutable=1`, which ignores the write-ahead
  log, so the portal checkpoints the WAL after every user/device write — new accounts are
  visible to Shelfmark immediately. Shelfmark also only starts once CWA is healthy: with no
  `app.db` present it would run with *no* authentication.
- **Per-user destination**: `INGEST_DIR=/dropbox` with `FILE_ORGANIZATION=organize` and
  `TEMPLATE_ORGANIZE={User}/...` — the per-user level is in the naming TEMPLATE, not in the
  destination path, because Shelfmark's entrypoint `mkdir -p`s the raw destination and a
  literal `{User}` folder would appear in the dropbox root. Every ebook lands in
  `library/dropbox/<username>/`, is tagged `owner:<username>` and atomically ingested within
  ~15 s. Limit its formats to epub, pdf and cbz.
- **Audiobooks** land in the same dropbox; an audio-only folder becomes one audiobook, tagged
  to its owner in Audiobookshelf automatically. A folder of ebooks is imported book by book;
  mixed or empty folders are parked in `.failed` with a reason.
- **Sources are opt-in** in Shelfmark → Settings.
- **Protection challenges** (the browser check some download sites put in front of a page):
  with **Operations → FlareSolverr** on, Shelfmark sends them to the shared FlareSolverr
  container instead of starting its own Chromium inside its 768 MiB fence. The switch is an
  environment value (`USING_EXTERNAL_BYPASSER`, from `FLARESOLVERR_ENABLED`), which in the pinned
  Shelfmark always wins over its Settings page — so turn it on and off in the menu, not there.
- Shelfmark refuses to start without CWA's `app.db` and reports unhealthy if its auth mode is
  ever anything but `cwa`.
- Exposure identical to the portal (Cloudflare → mTLS → optional Authelia → its session);
  `/api/auth/*` shares the Caddy login rate limit. Health: `http://127.0.0.1:8084/api/health`.

## Comics & manga (Library → Comics; docs/COMICS.md)
Readers get a **Comics** page: search a series (Western comics through Metron, manga, manhwa and
manhua through MangaUpdates), tick the issues or volumes they want. Nothing is read online; each
one arrives like a book:

| Where | How |
|---|---|
| Kobo (colour) | its existing link: a colour, fixed-layout copy made by KCC. Made automatically for readers whose Kobo syncs, or with **Make Kobo copy** on the comic's page |
| Kindle (Colorsoft) | **Send to Kindle** (or auto-send): a Kindle copy made at that moment, never stored; a big volume arrives in parts |
| Phone / iPad | My books → download the CBZ (Panels, Chunky, KOReader) |

- **Downloads go through Shelfmark**, as the reader: the seedbox, Shelfmark's ebook category and
  label, torrents kept seeding, Syncthing, the path mappings, the reader's dropbox. Nothing on the
  seedbox is new. The portal only chooses the release.
- **The right release**: a chapter never fills a volume request, the reader's language only (raws
  and other languages are dropped), the series and number must match (no spin-offs, no wrong
  year), a single digital volume beats a pack, Usenet beats a torrent at equal quality, a dead
  torrent is never picked. Nothing right: it looks again (1 h, 6 h, then daily, 30 days).
- **Family sharing**: an issue or volume someone has is added to the next reader's library.
- **CBR, CB7** are repacked as CBZ; the series and number are written into the file, so Calibre
  and the Kobo show them as a series.
- Setup: **Library → Comics** (a free Metron account's API key; ComicVine optional), and in Shelfmark tick
  **CBR** under Formats. Following a series and reading trackers (AniList) come in v5.8.

## Following & New for you (v5.8)
**Follow** a comic or manga series (its page under Comics), a book series or an author (the
**Following** page, from Hardcover: needs the admin's free Hardcover key under Library → Metadata
sources). Once a day each is checked; what has come OUT since (released, not merely announced)
shows on the reader's **New for you** list at the top of the portal, with one tap:
**Request** for a comic or manga volume, **Get it** for a book (v5.8.3, below), or **Pick in
Shelfmark** to choose a book's copy by hand. Nothing downloads by itself, and following a long
series never floods anyone: the first check only records what is already out. Readers who turned
mail on (Devices) get one digest; the admin's ntfy gets a daily count.

Every followed name opens its page: a comic series its volumes, a book series or an author (v5.8.3)
every book in order with release dates, which ones the reader has (linked), which ones the family
has (**Add to mine**, no download), which are requested and which are not out yet.

**Get it** (v5.8.3, `librarian/bookreq.py`): the portal does for a book what it does for a comic,
with three safeguards because a book's copies vary far more:
- **The family copy first**, then Shelfmark's own Prowlarr search, scored by `bookrel.py` (the
  title as a phrase with nothing left over that makes it another book, the author's surname, a
  single book: no packs, box sets, abridged copies or summaries; EPUB > AZW3 > MOBI, no PDF; the
  reader's language; Usenet or a seeded torrent).
- **1. The reader confirms the copy.** Requests shows what was found (release name, size,
  indexer, why) and nothing downloads until **Yes, that one**; **Not it** never offers that
  release name again, from any indexer. Advanced settings → requests → `BOOK_CONFIRM=sure`
  lets a certain pick (title and author in the name, a retail EPUB, the reader's language)
  download without asking; never after the reader turned one down.
- The chosen release is queued in Shelfmark as the reader, so the download, the seeding and the
  delivery are Shelfmark's.
- **2. The file is checked before the import**: its own title, author, language and ISBN (from
  inside the EPUB/MOBI/AZW3) and its format. An ISBN that is one of the book's editions in the
  reader's language (Hardcover) settles it. A file that does not match is **held**, not imported:
  the reader sees what it says it is and chooses **Keep it anyway** or **Not it: find another**.
- **3. Wrong book** on a delivered book's page takes it out of the reader's library, never counts
  that copy or release again, tells the admin, and looks again (asking first).

Nothing right: it looks again (1 h, 6 h, then daily, 14 days) and then says to Pick in Shelfmark.
A download that failed or never arrived tries the next release (asking first). Requests shows
each one, with a count in the menu while something waits for the reader; approvals apply as for
any request. Also on a book's page (Search): **Get it through Shelfmark** next to **Pick in Shelfmark**.

**Reading status** comes from Calibre-Web itself (the Kobo's Finished / Reading and page position,
KOReader, the web reader): My books, a book's page and a comic series show Read / Reading 45 %,
and a series shows the next one to read. A Kindle (Amazon has no API) and Panels or Chunky on an
iPad report nothing, so (v5.9) every book has **Mark: Read · Reading · Unread** and a comic series
**read up to here**; it is stored in Calibre-Web itself, where the trackers below read it.

**AniList**: with an AniList API client set (Library → Comics), each reader can connect their
AniList on **Devices**; manga volumes they finish count as read on their AniList list.
The number only goes up, and a series is matched by its exact title or not at all.

**Metron** (v5.9, Western comics; `librarian/metrontrack.py`): each reader connects their own
free Metron account on **Devices** (user name and API key, or password; kept in the portal only).
Issues they finish are marked read in their Metron collection with the date (Metron's collection
scrobbling). Only comics the portal found through Metron.

**Following chapters** (v5.9, manga from MangaUpdates): **Follow chapters, then volumes** on a
series. Each new chapter (MangaUpdates' latest chapter) is a New for you item, downloaded like a
volume when the indexers carry it (measured on the owner's: Chainsaw Man and One Piece yes,
Kagurabachi no; not found in 7 days, it says the volume will come). Chapters go into their own
Calibre series ('One Piece (chapters)'), so volume numbers stay clean on the Kobo. When the
volume is in the reader's library, Comics offers to remove the chapters it holds, pre-ticked from
MangaDex (only the MangaDex entry whose MangaUpdates link is this series); nothing is removed
until the reader says so.

**The book safeguards for comics** (v5.9): the reader confirms each copy on Comics (release name,
size, indexer, why; **Yes to all** per series); every arrival is checked against the request
(ComicInfo series, number, language; the name's language; at least 40 pages for a volume, 8 for
an issue, 5 for a chapter) and held when it does not match (**Keep it anyway** / **Not it**);
**Wrong comic** on a delivered comic's page. `COMIC_CONFIRM=sure` (Advanced settings → requests)
skips the question only for the exact digital issue or volume in the reader's language.

## 6.0: one address, one sign-in, and a home for every reader

**home.<domain>** (`templates/hub.html`, `templates/help/`) is the one address the family needs:
tiles for every site (the portal, My books, My audiobooks, Audiobookshelf, the library site,
Comics, Following, Shelfmark, Devices, Requests; for admins the dashboard and Uptime Kuma), a
setup checklist per reader (Kobo linked, Kindle address, notifications, Hardcover), and 13 guides
with how-tos and answers (getting started, Kobo, Kindle, phone and tablet, audiobooks, getting a
book, comics, following, reading status and trackers, notifications, account and security, FAQ,
admin). The sites themselves stay where they are. Behind the Authelia gate it is where the ONE
sign-in happens: its cookie covers every subdomain. **Admins always need a second factor;
readers sign in with their password** (`AUTHELIA_READERS_2FA=true` asks it of them too; the
Audiobookshelf sign-in follows the same rule). Deploy creates its DNS record.

**The portal's home page** (`librarian/home.py`) is now each reader's own: what they are reading
(the Kobo, KOReader, marks), what they are listening to (Audiobookshelf, with time left), the
next book in each series they are reading, and what arrived lately, with search on top.

**My books**: a cover grid or a list, filters (unread, reading, finished; books or comics), and
sorting (recently added, title, author, series order), all in the address, so a filtered view can
be bookmarked.

**Get the audiobook** (`librarian/audiorel.py`): the book safeguards for audiobooks. The portal
searches Shelfmark's audiobook categories, prefers M4B and unabridged (a narrator in the name is
not "another book"), asks the reader to confirm, and checks what arrives: its tags and its LENGTH
against Hardcover's for the book (read with mutagen, nothing decoded), so an abridged or partial
copy is held. **Wrong audiobook** on My audiobooks. The family's copy is shared first.

**Hardcover "Want to Read"** (`librarian/hcwant.py`, opt-in on Devices): books a reader adds to
their Want to Read list on Hardcover become Get it requests (ebook, audiobook or both), each
waiting for their yes (always: a Want to Read pick never downloads by itself, even with
`BOOK_CONFIRM=sure`). Turning it on records the list as it is at that moment and requests nothing
already on it; Devices offers to.

**Phone notifications for readers** (Devices, the free ntfy app): a private topic per reader on
ntfy.sh, or the server named in `READER_NTFY_URL` (Advanced settings -> mail): a book, audiobook or comic arrived, a copy waits for your
yes, a file needs checking, something you follow came out.

**The admin dashboard** opens with **What needs you** (approvals in the portal and Shelfmark,
held files, copies nobody confirmed, failed imports, stuck downloads, followed names failing,
tracker connections refused, worker health) and the week in numbers.

The portal now runs its own script (`static/app.js`, CSP `script-src 'self'`, never inline or
third-party): filters apply at once, typing filters My books as you go, Copy buttons, and
Requests refreshes itself while something is being looked for or downloaded.

**Send to an e-reader** (v5.9.1, `librarian/sendcode.py`): on the Kobo or Kindle, open its web
browser at `request.<domain>/send`; it shows a 4-character code (no login on an e-ink keyboard).
Type that code on a book's page (**Send to an e-reader**) and the e-reader's page offers the
download a few seconds later, straight into its library: no mail, no size limit. A Kobo gets a
KEPUB/EPUB, a Kindle's browser AZW3/MOBI/PDF (never EPUB: convert first). Only the browser that
showed the code can fetch the book (a secret cookie), a code lives 15 minutes and carries one book.

**Audiobook progress to Hardcover** (v5.9.1, `librarian/hcaudio.py`): with the Hardcover token a
reader already set on Devices, their Audiobookshelf listening progress goes to their own Hardcover
account (the book found by ASIN, ISBN, or exact title and author; Currently Reading, then Read
when finished, with the audiobook edition and the seconds listened), every 10 minutes when
something moved. A book already Read there is not reopened by a re-listen.

**Audiobooks on a phone or tablet**: the portal's **Audiobooks** page downloads any audiobook
the reader has (one file as it is, a folder as one ZIP) for any player; Audiobookshelf's app
keeps your place and can keep books offline too.

## Family sharing — one copy, many readers
Every reader sees only books carrying their own `owner:` tag. When a reader asks for a book
the family already has, the portal adds their tag to the existing copy instead of fetching it
again (`librarian/share.py`):
- **Shelfmark:** every reader's download arrives as a request (`REQUESTS_ENABLED`, switched on
  by Deploy once the portal's Shelfmark service login exists). The portal answers each within
  seconds: a family copy is given to the reader and the request is closed with the note
  "Already in the family library" (Shelfmark cannot mark a picked release done without
  downloading it, so it shows as *rejected*, with that note); anything else is approved at once,
  or, with approvals on, waits on the Pending card as before. Admins download directly in
  Shelfmark; their repeats are merged on arrival.
- **Arrivals** (dropbox, Shelfmark, e-mail) and **portal requests**: matched before anything is
  imported or downloaded; a match is merged, the duplicate file is dropped.
- **Ebooks:** the host job adds the second owner in Calibre (`tag_push` with `share=1`), reading
  every tag back: existing owners are never removed and an untagged book is never adopted.
  **Audiobooks:** the portal tags the Audiobookshelf item directly.
- **Matching:** ISBN or Calibre UUID, or the same title AND an overlapping author. Title alone
  never shares, and two candidates are never guessed between. Audiobooks match on the name.
- **Find a better copy** (a book's page, for any reader who has it, or the admin): a badly
  converted MOBI, a missing cover. For 7 days the next EPUB of that book that arrives (Shelfmark
  lets an EPUB release through instead of closing it, and asks for an EPUB otherwise; a dropbox
  or portal upload counts too) replaces the FILE inside the same Calibre book: owners, cover,
  corrected metadata and the Kobo's identity of the book stay, every reader who has it gets the
  new file, and formats made from the old one (the Kobo's KEPUB, a kept MOBI, conversions) are
  removed to be made again. The host job does the swap, reading the owner tags before and after.
- **Remove from my library** (a book's page): only that reader's owner tag comes off; everyone
  else keeps the book. The page first says how to delete the copies already on their devices
  (Kobo: *Remove → Remove from My Books*; Kindle: *Remove from Device*, plus Amazon's *Manage
  Your Content and Devices* for mailed books), because Calibre-Web's Kobo sync never removes a
  book it no longer shows (measured in the CWA v4.0.6 source, unchanged in v4.0.7).

## Keeping the VPS small (it has 80 GB; the seedbox has the space)
- **Books no reader has any more** are deleted from the server `LIBRARY_RELEASE_DAYS` (7) after
  the last reader removed them, or after every owner's account was removed. The host job
  deletes only if the book's owner tags are still exactly what the portal saw (a book someone
  got again in the meantime is kept), permanently (no Calibre trash on a small disk). A book that
  never had an owner (the admin's own, added in Calibre-Web) is never touched. `0` = never.
- **Seedbox downloads**: this server keeps its Syncthing copy for a week, then drops it and
  ignores it (the seedbox keeps seeding). Asked for again, Shelfmark finds the torrent complete
  and waits for the file; the portal passes what it is waiting for to the seedbox job, which
  stops ignoring that item, so Syncthing brings it back and it is handed over again.
- **Hourly** (scripts/disk-watch.sh): stale partial files, the journal (200 MB), Docker's build
  cache (7 days), dangling image layers left by rebuilds (never tagged images: Update keeps
  `:prev` for its rollback), apt's package cache, restic's cache. CWA's own copies of every
  imported/converted file are off by default (Library → Formats).
- **Daily**: old versions of the stack's own images (the Shelfmark or CWA an Update replaced,
  0.5-2 GB each). Kept: anything a container uses, the pinned tags, the `bookstack/*:prev`
  rollback images, and the versions Update would roll back to, for 7 days after an update and
  while one is unfinished. Other images on the box are never touched.
- **Weekly**: `apt-get autoremove --purge` (old kernels and orphaned libraries only, never a
  package installed on purpose), Audiobookshelf's daily log files after 14 days, and a failed
  better copy's staged EPUB after 14 days.
- **Memory, nightly at 03:45** (scripts/mem-tidy.sh): Calibre-Web, Shelfmark, Audiobookshelf and
  Syncthing keep the memory they once needed, and only a restart returns it. A service at or
  above `MEM_TIDY_PCT` (70) % of its own `mem_limit` is restarted, one at a time and back to
  healthy before the next, and only when idle: nothing waiting to import and the host job not
  writing (Calibre-Web), nothing queued or downloading (Shelfmark), nobody online
  (Audiobookshelf). A service that does not come back is an alert. The page cache is not
  "freed": the kernel already gives it back on demand, and dropping it only slows the next
  minutes. Measured peak for the whole stack ~2.2 GB of 4 GB.

## Seedbox — Shelfmark downloads on your seedbox, brought home by Syncthing (Library → Seedbox)
Shelfmark can search your seedbox's Prowlarr and send a reader's pick to the seedbox's SABnzbd or
rTorrent. Those download on the **seedbox**. The seedbox's own Syncthing sends the bookstack
folders (SABnzbd's bookstack categories, rTorrent's bookstack folder) to this server's Syncthing
container (compose profile `seedbox`, into `library/seedbox-sync`), and `scripts/seedbox-fetch.py`
(every 20 seconds) hands each finished, fully arrived item to `library/seedbox` (Shelfmark's
`/seedbox`) as hard links, where Shelfmark's remote path mappings file it into the reader's
dropbox. After a week this server drops its own copy (Syncthing is told to ignore the item
first), so its disk holds about a week of seedbox downloads.

**Nothing on the seedbox is ever moved, deleted or changed** (private trackers: strict seeding,
no hit-and-run). Enforced independently, and tested with two real Syncthing instances by
fingerprinting the whole seedbox tree after every run and every destructive act on this side
(`tests/seedbox-test.sh`):
1. every folder on this server is **Receive Only**: Syncthing never sends a change made here.
   Measured: deleting, editing and adding files here leaves the seedbox byte-identical, even
   with the seedbox side wrongly set to Send & Receive. The job checks it every minute; a folder
   found otherwise is **paused** at once, with an alert, and nothing runs until it is fixed;
2. the seedbox's side is **Send Only** (the setup screen says where to set it): it ignores every
   change from other devices;
3. the job never writes into the synced copy except to drop a week-old item it already handed
   over, and every request it sends to Syncthing goes through one allowlist (no revert, no
   override, no config change beyond pausing a folder);
4. rTorrent is asked one read-only question (name / complete / directory / finished). Torrents
   are handed over only once rTorrent reports them complete, at least 90 seconds earlier (the
   seedbox's file watcher must catch the last pieces), and keep seeding.

Timing: Shelfmark (v1.3.15 and v1.4.0, the latest as of 2026-09-28) cancels a download after 5 minutes
without progress, and its "Waiting for completed files" loop does not count, whatever Completed
Path Wait says. So the job runs every 20 seconds and hands an item over within ~2–3 minutes of
the seedbox finishing it. One that misses the window (a big audiobook) still arrives: press
Retry on it in Shelfmark and it imports at once (the torrent/job is already complete).

Shelfmark itself fetches only the small .torrent / .nzb file, from the seedbox's Prowlarr, to
hand it to rTorrent / SABnzbd. Those links sit behind the seedbox's login, so Library → Seedbox
gives Shelfmark that login for Prowlarr's host alone (`shelfmark/netrc/seedbox`, read-only,
`NETRC`); requests sends it nowhere else. Keep each Prowlarr indexer's **Redirect** off: then
Prowlarr fetches the .torrent from the tracker itself, from the seedbox's IP (IP-locked trackers
need that), and this server never talks to a tracker or a peer.

Shelfmark's own clean-up is pinned in `docker-compose.yml` where its web UI cannot change it:
after an import it keeps the torrent and the Usenet job (`PROWLARR_TORRENT_ACTION=keep`,
`PROWLARR_USENET_ACTION=copy`). It never removes a torrent on a cancel or failure (a hard rule in
its code); a Usenet job it added and that is cancelled or fails is deleted by Shelfmark, which
Usenet (no seeding) does not mind.

## Ephemera — optional, Tailscale-only (Operations → Ephemera)
Adds a "request it and auto-download when it appears" queue over Anna's Archive and a
newznab indexer mode. The portal's **Keep looking** does the same over the portal's own
catalogues; Shelfmark searches only when asked.
**Status (Sept 2026):** the upstream repo `OrwellianEpilogue/ephemera` and its image were
removed from GitHub. The overlay *builds* the last release (v1.3.1, Nov 2025) from a
community re-upload pinned to commit `e98e9944…`; treat it as unmaintained. It is reachable
only at `https://ephemera.<domain>` over Tailscale behind the admin password and files
everything to one CWA user's dropbox (`EPHEMERA_OWNER`), because it has no accounts of its own.

## FlareSolverr — the shared challenge solver (Operations → FlareSolverr)
One headless-Chromium container (`ghcr.io/flaresolverr/flaresolverr`, compose profile
`solver`) that both Shelfmark (when switched on) and Ephemera (always) use. It runs while
either one wants it, listens on `127.0.0.1:8191` only, and has no site in Caddy. Measured on
the pinned v3.5.2: ~50 MiB idle, ~450–500 MiB for each page being solved, back to ~125 MiB
after 20 page loads (no leak); two at once ~910 MiB inside its 1 GiB fence. Enabling it proves
Shelfmark reaches it *by name from inside the Shelfmark container*, which is the path that
matters, and the self-test repeats that check.

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
same name becomes `name (2).ext` instead of overwriting. Size caps: 200 MB ebooks (`MAX_EBOOK_MB`), 2 GB audio
(`MAX_AUDIO_MB`), PDFs over 250 MB are not tagged (`MAX_PDF_MB`, parked for users) — all
three in `.env`.
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
(Admin → Edit e-mail server) if you also want CWA's own Send-to-Kindle button, and one free
external monitor for "the whole VPS is down" (Operations → Monitoring asks for its URL).
Operations → Self-test checks the posture.

## Monitoring (Uptime Kuma, configured for you)
Deploy ends by configuring Uptime Kuma at `https://monitor.<domain>` (Tailscale only) — nobody
has to open its web UI and type monitors in. `monitoring/kuma_bootstrap.py` runs as a one-shot
container, reads its settings (passwords included) on stdin, and:
- creates Kuma's admin account on first run (`KUMA_USER` = your admin name, `KUMA_PASS`
  generated; both shown under **Operations → Monitoring**);
- adds the **same alert channels `scripts/alert.sh` uses** — your `NOTIFY_WEBHOOK` (ntfy-style
  plain text, like every other alert) and, when SMTP is set, e-mail to `ADMIN_EMAIL` — directly,
  not through the portal, so "the portal is down" can still be reported;
- watches, every minute: the portal, Calibre-Web, Audiobookshelf (still initialised — the state
  in which a stranger could become its root), Shelfmark and that it still demands library
  logins, Caddy's public listener; every 5 minutes, the full public path through Cloudflare;
  and each optional service that is on (qBittorrent, Authelia, Ephemera, FlareSolverr);
- adds **dead-man's switches** for the scheduled jobs — hourly self-test, disk watchdog, metadata
  push, Cloudflare IP refresh, nightly backup, and the canary journey when it is on. Each job reports in when it succeeds
  (`scripts/kuma-push.sh`); silence past its schedule is an alert;
- puts the nightly unattended reboot in a maintenance window, keeps 30 days of history (not
  Kuma's 180 — that is millions of rows in a database the backup snapshots every night), and
  never touches a monitor you added yourself. Its own monitors are put back to spec on every
  Deploy, Update and feature switch.

The **self-test now also runs every hour** (`bookstack-selftest.timer`) and reports to Kuma,
which alerts on the change and repeats once a day while it stays red; if Kuma cannot be told,
the hourly run falls back to `alert.sh`. The hourly run skips two probes that cost something
24 times a day (the factory-password login, which counts against Calibre-Web's per-name login
limit, and the remote restic listing). Every self-test pings `HEALTH_PING_URL`, the one check
that works when the whole server is gone.

## Security & ops menu (what's where)
- **Install & deploy**: Quick install, System, Tailscale, Configure, Cloudflare, Deploy, Backups, Alerts.
- **Users & devices**: list / add / Kindle / Kobo link / password / remove / repair / guide,
  Login lockouts (who is locked out of the portal, and release them).
- **Library**: Formats & conversion, Mail (SMTP + test), Sources & approvals, Shelfmark,
  Intake & dropboxes (webhook enable/show/disable), Torrents, Request queue, Parked files,
  Audiobookshelf rescan, isolation guide.
- **Security**: Authelia enable / disable / add user, Lock SSH to Tailscale, Reopen public SSH,
  fail2ban, Bans — list and release, SPF/DMARC, Cloudflare Access guide, Rotate the portal
  session secret.
- **Operations**: Self-test, Status, Restart / stop / start one service, Logs, Advanced settings,
  Check for updates, Update, Backups, Rotate the backup repository password, Alerts,
  Restore test, Restore from backup, Restore a single file, Monitoring (set up / repair Kuma,
  external check URL), FlareSolverr on / off, Ephemera enable / disable.

Backups are encrypted restic (7 daily / 4 weekly / 6 monthly by default — `RESTIC_KEEP_DAILY`
/ `_WEEKLY` / `_MONTHLY` in `.env`; `pre-update` snapshots kept 90 days; `restic check` weekly,
re-reading a different 1/52 of the repository each week so every byte is verified once a year;
a monthly restore test on the 1st) — keep the repo password safe. **Restore** lets you pick a snapshot and restore everything or only config +
databases; it checks free space first and restores in place. **Update** deploys the code from
the checkout it runs from, gates on container health, and can roll back to the previous
images and Caddyfile. Watch `df -h /srv`; audiobooks fill the disk fastest.

## When things go wrong
Everything here is a menu entry in `bookstack.sh` over Tailscale SSH. That is the admin
console; the web `/admin` page is a read-mostly dashboard and says so. Start with
**Operations → Self-test**: it names the menu entry for almost every failure it reports.

**"Nothing works this morning."** The server takes an unattended security reboot at 04:30, and
that is the one scheduled event that restarts everything while nobody is watching. It is now
checked automatically: a oneshot systemd unit waits for the containers to settle, runs the
self-test, and if anything fails it alerts through your Alerts channel and puts a line on the
first screen of the TUI. So the usual answer is that you already know. To look yourself:
`systemctl status bookstack-postboot`, `journalctl -u bookstack-postboot` for the full output,
or `$STACK_DIR/.postboot-selftest.log`. A result reading `KILLED part-way` means the check
itself hung or timed out — treat that as a failure, not as "no news".

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
(`LOCKOUT_*` in `.env`), separate from fail2ban. **Users & devices → Login lockouts** releases
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
new users created afterwards get one automatically — from the TUI or from the portal's /admin.
**Test in a browser right after enabling**; *disable* removes the gate instantly.

**One login (v5).** The portal and Calibre-Web sign the person in from Authelia's answer: Caddy
strips `Remote-User`/`Remote-*` and `X-Bookstack-Gate` on every path, and adds
`X-Bookstack-Gate: <GATE_SECRET>` only to requests Authelia let through. The portal trusts
`Remote-User` only beside that secret; Calibre-Web (which cannot check a secret) is reachable
only through Caddy and from the host, never from another container (each has its own network).
Logging out of the portal ends the Authelia session too. A password changed on the portal's
Devices page is queued as a PBKDF2-SHA512 hash, written into Authelia's user file by the host
(`scripts/gate-sync.py`, triggered by a systemd path unit) and picked up by Authelia without a
restart.

**Shelfmark** runs in its header-login mode behind the gate: the reader Authelia names is the
Shelfmark account of the same name (measured on v1.3.15: switching modes keeps every account),
admin rights come only from Authelia's `admins` group, which bookstack keeps equal to Calibre-Web's
admins, and a request without the gate's identity is refused. **Audiobookshelf** has no header
login, so Authelia is its OpenID Connect provider: its web page goes straight to Authelia (already
signed in, no consent screen) and back into the reader's EXISTING account, matched by username,
never auto-created, with its tag restriction untouched. Its apps keep their local login. Turning
the gate off puts all of them back on their own logins.

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
bash tests/monitoring-test.sh  # the Kuma bootstrap against the REAL pinned Uptime Kuma (~2 min)
bash tests/caddy-build-test.sh # caddy/Dockerfile: the Caddy release, its plugins, the full Caddyfile validates
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
- `scripts/` — Cloudflare-IP firewall sync, encrypted backup and `prune.sh` (retention with a
  separate key), restore test, disk watchdog, alerting, **selftest**, the host metadata push,
  `synthetic.py` (canary journey), `gate-sync.py` (passwords into Authelia), heal, cert and
  update watches
- `monitoring/` — the Uptime Kuma bootstrap (monitors, channels, push tokens)
- `configs/fail2ban/` — jails + Caddy filters (login and device-auth paths)
- `tests/` — unit runner, installer harness, end-to-end UAT + driver
- `docs/` — deployment checklist, research sweep (`RESEARCH-GAPS.md`), pending decisions

## VPS sizing (measured)
**The deployed and supported plan is 2 vCPU / 4 GB RAM / 80 GB NVMe / 10 TB (X4).** It is not
a compromise: the numbers below were measured on the real stack with a 5,015-book library,
and they replace the desk estimates this section used to carry.

| Measured on the real stack (5,015 books) | |
|---|---|
| Containers idle | **563 MiB** |
| Containers at peak, full end-to-end run driving every journey | **1,587 MiB** |
| Largest single container (calibre-web, against its 1600m fence) | 562 MiB |
| Plus Debian + systemd + sshd + tailscaled + fail2ban + dockerd | ~400–500 MB |
| Plus Uptime Kuma and the real xcaddy Caddy | ~150 / ~55 MB |
| **On the 4 GB box: idle / peak** | **≈ 1.2 GB / ≈ 2.2 GB** |
| Optional, measured 2026-09-25: Ephemera idle / FlareSolverr idle / per page solved | 58 / ~50 / ~450–500 MiB |
| **With Ephemera + FlareSolverr on, worst case (two solves at once)** | **≈ 3.3 GB** |

So RAM has about 1.8 GB of headroom at peak, with the swap the installer creates untouched.
The `mem_limit` fences in `docker-compose.yml` are what keeps one runaway container from
taking the box down; they are sized for this plan and are not to be retuned without a new
measurement showing real failure on 4 GB.

**Disk is the binding constraint, not memory.** On 80 GB, after the OS, images, swap and
ebooks, roughly 50 GB remains for audiobooks — 100–150 titles. Keep audiobooks under ~40 GB.
(Ephemera and FlareSolverr fit in RAM — see the table — and their images take ~1 GB of disk.) Watch `df -h /srv`, and watch inodes
as well: one directory per book plus covers, formats and multi-part audiobooks exhausts an
inode table long before it fills the disk, and the symptoms are identical.
`scripts/disk-watch.sh` and **Operations → Self-test** now check both and say which tripped.

**CPU, not RAM, is the tight resource on 2 cores.** A CWA library-wide KEPUB conversion
sustains 107.8 % CPU — more than one of the two cores — for as long as it runs. Batch work
(Convert Library, a first Audiobookshelf scan) belongs overnight, not beside someone reading.
Backups run at 01:00 and the unattended-upgrades reboot at 04:30; they are already staggered.
One CPU worry that is *not* real, measured so it stops being re-raised: Caddy's
`encode zstd gzip` compresses text only. Against caddy 2.11.4 with `Accept-Encoding: zstd,
gzip`, HTML and JavaScript came back zstd-compressed (100,000 bytes → 27) while `book.epub`,
`audio.mp3` and a plain `application/octet-stream` came back with no `Content-Encoding` and
byte-for-byte identical. EPUB, KEPUB, PDF, CBZ, MP3 and M4B are never compressed on the way
out, so there is no CPU to reclaim there and no `encode` allowlist to add.

**Bandwidth (10 TB/month) rules out any egress worry.** A household streaming audiobooks,
syncing Kobo and Kindle, and pushing an incremental restic off-site uses a few hundred GB a
month — under 5 % of the allowance. Nothing here needs rationing or a local-only fallback.

The 4-core / 8 GB / 160 GB plan (X8) is worth its extra EUR 50/year only when the library
outgrows 80 GB, or when the overnight batch jobs stop fitting in the night — not for RAM.
The 1-core / 2 GB plan is not viable. Upgrade signals and the full working are in
`docs/RESEARCH-GAPS.md` section 2.

## Known limits
- Isolation is visibility, not separate storage — admin sees all files.
- Cloudflare terminates TLS at its edge (price of the DDoS layer).
- Send-to-Kindle routes through Amazon; Kobo/OPDS talk only to your server.
- Authelia password resets are separate from library passwords (re-add the user in
  Security → Authelia after a library password reset).
- Ephemera is unmaintained upstream; only Shelfmark is the long-term companion.
