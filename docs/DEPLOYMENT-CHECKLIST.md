# Deployment checklist (pre-flight for the first VPS install)

Status as of 2026-09-22 (v4.3). Items marked **on the VPS** cannot be exercised on a laptop
and are covered by `Operations → Self-test` after deploy.

## The plan, and what it commits you to (read before ordering)

**Deployed and supported: X4 — 2 vCPU / 4 GB RAM / 80 GB NVMe / 10 TB, for a household of
3–4 readers.** This is a measured choice, not a budget compromise: the stack idles at ≈ 1.2 GB
and peaks at ≈ 2.2 GB of the 4 GB (measured on a 5,015-book library; see `README.md` → VPS
sizing). RAM is not the constraint. These three conditions are what the plan buys, and they
are operative guidance, not a footnote:

- [ ] **Audiobooks stay under ~40 GB.** 80 GB is the binding constraint — after OS, images,
      swap and ebooks, ~50 GB remains. Check `df -h /srv` *and* `df -i /srv`: an exhausted
      inode table looks exactly like a full disk while `df -h` still shows free space.
- [ ] **Browser-based challenge solving goes through ONE FlareSolverr.** Ephemera and
      Shelfmark's protected sources both use it (Operations → FlareSolverr). Measured
      2026-09-25: ~50 MiB idle, ~450–500 MiB per page being solved, fenced at 1 GiB — with
      Ephemera on, ≈ 3.3 GB worst case on the 4 GB box. It fits; it is the largest optional
      cost. (This line used to say "Ephemera stays off, X8 only" — a desk estimate.)
- [ ] **Batch work runs overnight.** CPU, not RAM, is what 2 cores run short of: a CWA
      library-wide conversion sustains 107.8 % CPU. Convert Library and a first Audiobookshelf
      scan belong at night. Backups (01:00) and the auto-reboot (04:30) are already staggered.

Bandwidth is a non-issue at 10 TB/month: audiobook streaming plus off-site restic is a few
hundred GB. Move to X8 (4c/8 GB/160 GB) when the library outgrows 80 GB or the overnight jobs
stop fitting in the night — not for memory. `docs/RESEARCH-GAPS.md` §2 has the upgrade signals.

## Verified locally (this machine, Docker/OrbStack running)
- [x] Repo layout matches what `bookstack.sh` copies; tests are excluded from the copy.
- [x] `bash tests/run-unit.sh` — portal suite inside the shipping image, against schemas
      dumped from the real CWA `app.db` / `cwa.db` and Calibre `metadata.db`.
- [x] `bash tests/tui-test.sh` — installer logic: `.env` quoting round trip **and** a real
      container receiving the values byte-for-byte, Configure, file copy, Caddyfile render,
      Authelia gate inject/remove, user add/kindle/kobo/passwd/remove/repair, formats, mail,
      sources, deploy gating, Ephemera enable/disable.
- [x] `bash tests/stack-test.sh` — end-to-end on the real containers (see README → Testing).
- [x] Custom Caddy image builds (xcaddy: cloudflare DNS, ratelimit, cloudflare-ip) and the
      rendered Caddyfile validates with it, with and without the Authelia gate.
- [x] Rendered Authelia configuration validates with `authelia validate-config`.
- [x] All three compose combinations render.
- [x] Shelfmark image exists on GHCR; CWA-auth, `{User}` destination, `/api/health` and
      `/api/auth/login` confirmed against upstream docs/source and the live container.
- [x] Ephemera: upstream repo + image confirmed gone; pinned fork commit exists.

## Verified locally in v4.1 (end-to-end run, ~100 checks, five green runs)
- [x] Simulated Kobo device: `/v1/library/sync` returns only the owner's books per token.
- [x] Intake webhook pull, Shelfmark-style dropbox drop, e-mailed attachment (plain IMAP) all
      end up tagged and imported; Send-to-Kindle and auto-Kindle mail captured by a real SMTP.
- [x] Authelia gate with the production snippet: browsers redirected, `/kobo/*` and `/opds`
      bypass, Authelia accepts the user file written by the installer.
- [x] Audiobookshelf bootstrapped by `python -m abs init`; users created tag-restricted; the
      worker tags a new audiobook to its owner automatically; per-user visibility holds.
- [x] Lockout (401 → 429), daily quota, CSP headers, audit trail, approval/denial e-mails,
      self-service password change propagated to CWA and ABS.
- [x] No portal worker crash and no container restarts during the run.
- [x] Ephemera overlay builds (inline Dockerfile) and the image serves `/health`.

## Verified locally in v4.2 (research sweep + adversarial review)
- [x] Reviewer blockers fixed and regression-tested: `caddy hash-password` fed a
      newline-terminated password with the result checked for a bcrypt/argon2 prefix; the
      Caddyfile refuses to render with an empty admin hash; `abs_initialised` fails closed;
      a failed gate injection aborts Configure/Restore/Authelia-enable with a message instead
      of starting Caddy on a half-written file; restic.env is shell-quoted and read back.
- [x] Portal: uploads, mail-in and dropbox writes go through `place_in_dropbox` (O_EXCL,
      O_NOFOLLOW, atomic rename) so a pre-planted symlink is never written through; dropbox
      symlinks and linked folders are parked with an error row; unsupported files are parked,
      not deleted; a transient app.db error keeps sessions; PDF owner tag lands in Info + XMP.
- [x] Edge: rendered Caddyfile validates with the built image with and without the gate;
      bypasses are anchored case-sensitive `path_regexp`; access logs redact Kobo tokens and
      `?token=` JWTs and drop Authorization/Cookie/X-Intake-Token/X-Auth-Key; client-sent
      `Remote-*` headers are stripped; `/robots.txt` answered before the gate.
- [x] End-to-end run found that CWA's import-time Kindle EPUB fixer strips the ComicBookInfo
      comment from CBZ files (Calibre 9.1 reads comic metadata from nowhere else), so tagged
      comics imported untagged and invisible. Shipped default is now fixer OFF; the portal
      applies the language/encoding fixes to the mailed copy instead; with the fixer ON the
      portal reports comics as `needs-tag`. Covered by unit tests and the end-to-end run.
- [x] Installer harness 158/158, portal suite 115/115 (15 new regressions), end-to-end run 11
      green: 147 checks, no worker crash, no container restart (see README → Testing).
- [x] Restore test restores only config + DB snapshots, beside the stack (not tmpfs).

## Verified locally in v4.3 (fan-out audit, fixes, synthetic journeys)
- [x] 77 verified findings fixed across installer, wiring, portal, journeys and operations
      (see README → What changed in v4.3); portal suite 138/138, installer harness 263/263.
- [x] Rendered Caddyfile validates with the new pinned Caddy build for torrents/Ephemera on
      and off, with and without the Authelia gate; every compose combination renders.
- [x] End-to-end run on real containers green after the fixes (see the commit message).

## Verified locally in v4.4 (admin-coverage audit, fixes, post-fix review)
- [x] A 38-agent audit built an independent list of every task this stack's admin faces over
      its life (100 tasks) and classified each against both consoles. Verdict: the whiptail
      TUI is the admin console; the web `/admin` page is a dashboard. The capability gaps it
      found are now TUI entries: unban (fail2ban **and** the Cloudflare access rule), clear a
      login lockout, restart/stop/start one service, reopen public SSH, restore a single file,
      rotate the restic password safely, the full request queue, parked files, an ABS rescan,
      and an editor for the 21 tunables that previously had no writer.
- [x] **Debian 12 backups actually run.** apt ships restic 0.14, which has no `--retry-lock`:
      every scheduled backup failed while setup reported success. Both scripts and the TUI now
      probe for the flag; proven in a real `debian:12` container with restic 0.14.0 and again
      against 0.18.
- [x] An independent review of the combined diff found 16 issues, two of them regressions this
      round introduced (the portal never read the disk-full pause flag it was documented to
      honour; the audiobook placer deleted the reader's ZIP before the import could fail).
      All 16 are fixed and covered by tests.
- [x] Suites after the fixes: installer harness **440 passed**, portal suite **233 passed**,
      end-to-end on real containers **163 checks / 0 failed** (no worker crash, no container
      restart, peak 1325 MiB). ruff clean; `bash -n` clean on every script; all 5 compose
      combinations render; **all 8 Caddyfile variants** (torrents x Ephemera x Authelia)
      validate against the pinned Caddy build, with `auth.` rendered only when the gate is on.

## Verified locally in round 4 (stack, Caddy and backups)
- [x] **Closed an unauthenticated open relay.** `books.<domain>/api/v3/*` and
      `/api/UserStorage/*` were proxied verbatim to `https://readingservices.kobo.com` for
      anyone, with the caller's method, headers and body and the upstream's response returned
      unchanged — reproduced against the pinned CWA image (the reply carried kobo.com's own
      `Set-Cookie` and `CF-RAY`). Both prefixes now 403 in `caddy/Caddyfile.template`, in the
      same matcher as CWA's unauthenticated admin jobs and its `;`-parameter companion.
      Proven behind a real Caddy with the pinned CWA image: all relay paths (including
      `/api/V3/…` and `/api/v3/x;y`) 403 with and without the Authelia gate, while `/login`,
      `/opds`, `/kosync`, `/kobo/<token>/v1/initialization` and the web UI are unchanged.
      No device is affected: CWA only advertises itself as the Kobo reading-services host when
      Hardcover annotation sync is on, which it is not.
- [x] **Shelfmark bumped v1.3.7 → v1.3.15** after running both side by side against the same
      CWA `app.db`: healthcheck still matches `auth_mode: cwa`, login with a Calibre-Web
      account still works (admin flag carried, wrong password 401), and `organize` mode with
      the `{User}` templates produces byte-identical output — one folder per audiobook,
      ebooks flat in the user's dropbox. v1.3.12's "preserve multi-file audiobook folders" is
      a *new* mode (`rename_and_group`) this stack does not use; `organize` was already right.
      Its new 300 s release-search timeout is pinned to 90 s, under Cloudflare's 100 s origin
      limit, so a slow search reports its real cause instead of a Cloudflare 524.
- [x] **Inodes are monitored, not just blocks.** `scripts/disk-watch.sh` and
      `scripts/selftest.sh` take the worse of blocks-used and inodes-used, and the alert names
      which tripped — the remedies have nothing in common. A reading that is not a 0–100 number
      (no fixed inode table, busybox `df`) is treated as unknown, never as full.
- [x] **Backup verification now covers the whole repository.** `--read-data-subset=5%` re-picked
      its 5 % at random weekly, so no pack was ever guaranteed to have been read. Now
      `n/52` with the counter rotating in `/etc/bookstack/backup.state`: every byte read once a
      year, weekly transfer down from 5 % to ~1.9 %. The counter advances only after a check
      that passed, is backed up with the rest of the host state, and Self-test reports it.
- [x] **The Kobo/KEPUB claim was false and is corrected** in `README.md`; the real enablement
      path and the HTTP-500 trap are recorded in `docs/DECISIONS-PENDING.md`.
- [x] Verification: `bash -n` clean on every script; **all 5 compose combinations** render;
      **all 8 Caddyfile variants** (torrents × Ephemera × Authelia) validate against a fresh
      build of `caddy/Dockerfile`; ruff clean on the Python in `authelia/` and `tests/`.

## Verified locally in v4.5 (metadata, identification, restore)
- [x] Restore now brings back the three host files nothing else regenerates (sshd drop-in,
      sysctl, daemon.json); sshd is validated before any reload and a failing drop-in is removed.
- [x] Book, author and series pages; the provider chain with its failure matrix (dead, soft
      miss, rate limit, bad request, slow, unexpected exception) each covered by a test.
- [x] Arrivals identified by the file's own OPF (title, author, ISBN / Calibre UUID).
- [x] `calibredb set_metadata` proven in the pinned CWA image to update title, title sort,
      authors, series and series index while leaving `owner:` untouched.
- [x] Internet Archive downloads verified against the published size and SHA-1.
- [x] Suites: installer 510, portal 332, end-to-end 181 checks / 0 failed on real containers.

## Verified locally in v4.6 (monitoring, FlareSolverr)
- [x] `monitoring/kuma_bootstrap.py` against the real pinned Uptime Kuma 1.23.17 (41 checks,
      `tests/monitoring-test.sh`): first-run account, idempotent re-run, feature monitors added and
      removed, a hand-made monitor untouched, drift put back, 30-day history, reboot window, live
      push tokens, a DOWN push delivered ntfy-style to a webhook AND as e-mail through GreenMail,
      `/metrics` readable with the admin login, wrong credentials exit 2.
- [x] Shelfmark v1.3.15 with `USING_EXTERNAL_BYPASSER=true` fetches through a FlareSolverr
      container (seen in FlareSolverr's log) and runs no Chromium of its own.
- [x] FlareSolverr and Ephemera memory measured (README → VPS sizing).
- [x] Found and fixed on the way: the post-boot unit's `OnFailure=` target was only written by
      the Backups step; a missing reboot-time line aborted Deploy under `pipefail`; the Shelfmark
      login probe's fixed name would have hit its 10-failure lockout every 10 hours once hourly.
- [x] Installer suite 564 / 0.

## Verified locally in v5 (metadata engine, conversions, backlog L01-L22)
- Portal unit suite 482 passed; installer suite (tests/tui-test.sh) green; full stack test
  (tests/stack-test.sh) on the real containers, incl. section 15 (metadata-first search, EPUB ->
  AZW3, cover and description fill, Shelfmark approvals) and the canary journey (import 20 s,
  book removed again; a broken step alerts with its name and pushes DOWN to Kuma).
- L01: per-service networks; Shelfmark cannot open a connection to Calibre-Web, ABS, the
  portal or Authelia; capabilities dropped (CWA, Shelfmark, qBittorrent keep CHOWN, SETUID,
  SETGID, DAC_OVERRIDE, FOWNER; ABS and FlareSolverr none) — every journey still passes, and
  qBittorrent 5.2.3 / FlareSolverr 3.5.2 were started the same way and worked.
- L02: the seeded qBittorrent password logs in on 5.2.3 (PBKDF2 in its own format).
- L05: behind the gate the portal and Calibre-Web open without a second login; Remote-User is
  ignored without the gate secret and stripped on bypassed paths; a portal password change
  reaches Authelia (PBKDF2-SHA512, verified by Authelia 4.39.28) without restarting it.
- L14/L15: exercised against stubs only (Cloudflare API, restic/B2) — the VPS checks below
  are the real proof.

## Verified locally in v5.6 (admin alerts, daily disk summary, Caddy 2.10.2)
- Unit 557 passed, installer 776 passed, seedbox 47 passed, end-to-end 278 checks / 0 failed
  (the Authelia gate on caddy:2.10.2, `python -m abs backups` read back from the real
  Audiobookshelf 2.36.1), tests/caddy-build-test.sh 8 passed: caddy/Dockerfile builds Caddy
  v2.10.2 with its three plugins on the 2.11.4 builder's Go, and the full production Caddyfile
  (every optional site, gate injected) validates on it.
- Shelfmark v1.4.0's queue payload read from its source (orchestrator._task_to_dict): `id`,
  `username`, `status_message` carries the error. The failed-download notice parses exactly that.
- Uptime Kuma `:1` checked: still 1.23.17 inside, rebuilt 2026-09-16 on a newer base than the
  `1.23.17` tag, so it stays `:1` (2.x is v5.8). CWA's archived-book cleanup is already on by
  default in v4.0.7 (daily 03:00): nothing to set.
- On the VPS after Deploy: the ntfy app shows the next request with an emoji and, if it waits
  for approval, a Review button; tomorrow at 09:05 the first disk summary arrives ("First
  report"), the day after it shows the growth; Self-test shows "daily disk summary scheduled".

## v6.1.0 (removal reaches the Kobo; audiobooks removed and released; landscape comics)
- Remove from my library now takes the book off the reader's Kobo at its next sync: the portal does
  what Calibre-Web's own Archive does (CWA v4.0.8: archived_book for the reader, the book off
  kobo_synced_books; the next sync sends it with IsRemoved). A Kobo that syncs only chosen shelves
  has it taken off those shelves (CWA's two-way sync). Their owner tag waits for that sync (the
  sync ignores books a reader cannot see), at most 7 days. Asked for again meanwhile, it stays.
- Remove from my audiobooks (My audiobooks); an audiobook nobody has any more is deleted from the
  server after LIBRARY_RELEASE_DAYS, like books. During that countdown a book or audiobook asked
  for again is given back, never downloaded again.
- A copy waiting for a reader's yes is closed when the book (or comic, or audiobook) is theirs by
  then (the Peanuts was offered again after it arrived). Requests shows a removed book as removed.
- Landscape comics (The Complete Peanuts) are rotated on the Kobo and the Kindle, not cut in half;
  Remake Kobo copy on a comic's page replaces an old copy.
- After Deploy: on The Complete Peanuts' page press **Remake Kobo copy**; after the next Kobo sync
  it shows whole, turned pages (hold the Kobo sideways).
- Tests: unit 815 passed, installer 823 passed, end-to-end 334 checks / 0 failed, lint clean. On the
  real CWA v4.0.8 stack: a book on alice's (simulated) Kobo, removed on the portal, is sent to the
  Kobo at its next sync with IsRemoved; only then does her owner tag come off, and it is never
  offered to that Kobo again. KCC measured on a landscape test book: 21 halves by default, 11 whole
  turned pages with -r 1.

## v6.0.1 (large comics and audiobooks; no credentials in error text)
- Found on the server the day v6.0.0 went live: The Complete Peanuts v01 (a 339 MB CBR) was refused
  at import, because comics were held to the ebook cap (200 MB). Comics now have `MAX_COMIC_MB`
  (2 GB) and audiobooks `MAX_AUDIO_MB` 4 GB (was 2 GB); both caps also bound what the searches
  pick. A large comic, like an audiobook, waits for disk space (twice its size + 2 GiB) before it
  downloads, keeping the reader's yes; an archive is sized from its listing and refused BEFORE it
  is unpacked when it claims more than three times the cap or more than the disk has.
- "v01 - 1950 to 1952" was read as volumes 1 to 1950 (a pack, skipped): a range whose end is a
  year far past its start is a year. Names that start with " - " (Shelfmark's "Author - Title"
  with no author) are cleaned.
- A failed Usenet download's error quotes the whole SABnzbd URL (login in it) and the Prowlarr link
  (its API key in it). It reached the admin's alerts as it was: every alert, push, e-mail and
  Shelfmark error now has logins, API keys and tokens replaced by ***. Shelfmark's OWN page still
  shows its raw error to whoever requested the download: keep credentials out of the SABnzbd URL
  where the seedbox allows it.
- The Peanuts, imported under the 1024 MB stopgap, showed the rest: its title kept the leading
  "-", Calibre took "1950 to 1952 (2004) (digital)..." as its AUTHOR ("X - Y" in a file name), and
  KCC failed on the "-" at the start of the file name. Now: a comic nobody asked for is titled
  "The Complete Peanuts Vol. 1 (2004)"; KCC always gets `comic.cbz`; the Kobo copy is one file
  (KCC measured on 368 MB / 268 pages and 687 MB / 700 pages at ~1.2 GB of the 1536 MB cap, see
  docs/COMICS.md); a request whose comic is in the reader's library by series and number is closed
  (never downloaded again). Queues for several readers: Kobo copies and dropbox imports take the
  readers in turn; large downloads reserve their disk space and wait their turn.
- Self-test warns when Shelfmark's failed downloads show its SABnzbd API key holding a web address,
  and the admin's alert for such a failure says so.
- Tests: unit 799 passed, installer 823 passed, end-to-end 326 checks / 0 failed (its comic now
  arrives as " - E2E Manga v03 - 2019 to 2021 (Digital) (e2e).cbz": matched as volume 3, KCC made
  the Kobo copy under the plain name in 18 s), lint clean. Caddy build, monitoring, Kuma upgrade and
  seedbox suites are unchanged since v6.0.0 (8 / 41 / 8 / 47 passed).
- After Deploy:
  - Operations -> Advanced settings -> uploads: MAX_COMIC_MB 2048, MAX_AUDIO_MB 4096; set
    MAX_EBOOK_MB back to 200 (raised to 1024 by hand as the Peanuts stopgap).
  - Advanced settings -> comics: KCC_MEMORY 1536m, KCC_KOBO_MAX_MB 1024.
  - A parked comic in library/dropbox/<user>/.failed/ goes back with `mv .failed/*Name*.cbr .`.
  - Calibre book #12 (the Peanuts imported as "- The Complete Peanuts ..."): in Calibre-Web (books.,
    as admin) Edit metadata -> Title "The Complete Peanuts Vol. 1", Author "Charles M. Schulz",
    Series "The Complete Peanuts", number 1; keep every tag (Comics, owner:corgibot) -> Save.
    Calibre renames the files; the next pass closes the comic request as done (series + number),
    and the Kobo copy is made under the new plain name.

## Verified locally in v6.0 (home., one sign-in, home page, audiobooks, Want to Read, notifications, dashboard)
- Unit 771 passed, installer 823 passed, monitoring 41 passed (Kuma 2.5.5-slim), Kuma upgrade 8 passed,
  Caddy build 8 passed (the production Caddyfile with the gate on every site validates), seedbox 47 passed,
  end-to-end 326 checks / 0 failed, lint clean.
- Synthetic v6.0 journey on the real stack (tests/e2e_driver.py 14c): home. is the production site
  (gate first, then only the start page; /login, /admin, /send, /intake, downloads -> request.), one
  sign-in reaches it; with the production Authelia rules a reader passes with a password and an admin
  with a password alone is stopped on home. and request.; an e-reader gets a code without a login, a
  book sent to it downloads, Send another book gives a new code that survives the page's refresh; Get
  the audiobook is stored and shown as an audiobook; phone notifications and Want to Read switch on/off;
  the admin dashboard and the Help link. The canary journey (scripts/synthetic.py) passed as well.
- UI/UX pass on a local portal with realistic data (desktop and a 375 px phone): titled placeholder
  covers, requests that need an answer first, buttons that wrap on a phone, two tiles per row on the
  start page, a Help link in the nav, the dashboard's Health card given room, reader-facing wording
  where admin instructions showed, a sign-out that keeps where the reader was going; no console or
  CSP errors.
- Authelia 4.39.28 validates the new rules (group:admins two_factor, then one_factor for the rest)
  and the Audiobookshelf client's custom authorization policy "family" (validate-config).
- Audiobook picker on real-world names: M4B unabridged beats MP3, "narrated by Ray Porter" is not
  another book, an EPUB / an abridged copy / a Books 1-3 collection / another title are refused.
- mutagen 1.48.1 added to the portal (the only new dependency; hash-locked, all other pins as before).
- Pre-release audit (10 auditors, each finding re-checked by a skeptic): 88 confirmed findings, about 45
  distinct, all fixed: audiobook Keep for folders, single-file audiobooks checked, arrivals matched to
  the best request, held files kept on the dropbox mount (a rename, not a copy into /state) and
  dropped after 14 days, a disk check before an audiobook downloads, audiobook ownership per reader,
  a rejected copy never handed back on arrival, companion PDFs with audiobooks, Want to Read seeding,
  Deploy/Update re-render and recreate Authelia (home. would otherwise answer 403 with the gate on),
  AUTHELIA_READERS_2FA really reaching Authelia, home. restricted to the start page (every other path
  -> request., where the rate limits, fail2ban, Turnstile and Access apply), the home DNS record never
  repointing an existing one, admin portal rights through the gate needing the admins group, the Kuma
  1.x copy made atomically and never overwritten on a retry, pins offered only as upgrades, -slim
  update checks, per-entry cache retention, cached home-page Audiobookshelf calls, no duplicate pushes,
  removed accounts' requests closed, and the texts that promised more than the code did.
- Second audit round (6 reviewers over the fixed tree, every finding re-checked by a refuter): 30
  confirmed, all fixed. The two that mattered most: the gate on home. never ran (Caddy orders handle
  before route; the site is now ONE route, proven with `caddy adapt`), and a password-only reader
  with the Calibre-Web admin role could post the portal's own login form behind the gate to get admin
  rights (the group rule now holds on every gated request). Also: Install -> Cloudflare repointed an
  existing home. record (it now goes through the same free-name check, and bookstack's own record
  carries a comment so a restore still moves it); a taken home. sends every start-page link to
  request./hub (HOME_URL); gate-sync keeps the admins group in step with Calibre-Web roles every 10
  minutes; Keep never downloads the book again; an audiobook still downloading is never replaced;
  a confirmed audiobook waiting for disk space is not offered again; Want to Read never downloads
  without a yes, records the list the moment it is switched on, and never mistakes an old book for
  a new one; /send?new=1 no longer makes a code every 5 s; series numbers like 1.05; the ebook and
  the audiobook shown apart on series pages; Update creates home. too; Kuma skips a home. that is not ours.
- Upgrading from v5.9.x: readers who had to use a second factor at the sign-in page now sign in with
  their password (the chosen 6.0 policy); set AUTHELIA_READERS_2FA=true (Advanced settings -> lockout)
  to keep asking them. Deploy re-renders Authelia and re-syncs its admins group from Calibre-Web roles.
- On the VPS after Deploy:
  - Deploy (and Update, and Install -> Cloudflare) creates home.<domain> in Cloudflare when that name
    is free. A record of another service is left alone: the summary says so and the start page is then
    https://request.<domain>/hub (every link, Authelia and Kuma follow). Open it: tiles, your setup, guides.
  - Operations -> Update: answer Yes to the newer pins if it offers any.
  - With the Authelia gate on (Security -> Authelia): sign in once at home., then open books.,
    audio., request., shelf.: none asks again. Readers need only their password; admins a second factor.
  - The portal's home page: Continue reading / listening, Next in your series, Recently added.
  - A book's search page: Get the audiobook -> confirm on Requests -> it arrives in Audiobookshelf.
  - Devices: Phone notifications -> Turn on -> subscribe in ntfy -> Send a test.
  - Devices (with a Hardcover token): Want to Read -> on (the note says how many were already on the
    list); add a book on Hardcover; within 10 min it waits for your yes on Requests.
  - Admin: What needs you.

## Verified locally in v5.9.1 (Kuma 2.x, new Kobos, send to an e-reader, audiobooks to Hardcover)
- Unit 711 passed, installer 803 passed, monitoring 41 passed on louislam/uptime-kuma:2.5.5-slim,
  Kuma upgrade 8 passed (1.23.17 -> 2.5.5 on the same data: migrated in 38 s, nothing added or
  deleted, the channel, the reboot window and the push tokens intact), end-to-end 290 checks / 0 failed on CWA v4.0.8.
- CWA v4.0.8 (2026-09-28) answers /v1/user/add-device and /v1/auth/refresh, so a Kobo that was
  never paired, on firmware 4.38, can pair (CWA #1476). Operations -> Update now offers every pin
  this release moved (CWA v4.0.8, Kuma 2.5.5-slim) in one question.
- A Read / Reading / Unread marked on the portal now reaches the Kobo: Calibre-Web's Kobo sync
  sends a state only when kobo_reading_state.last_modified moved, and its own "Mark as read" gets
  that from an ORM hook raw SQL skips (cps/kobo.py, ub.py at v4.0.7); the portal now bumps it, or
  creates the state as Calibre-Web does, never touching the Kobo's own position.
- Kuma 2.x: the bootstrap uses uptime-kuma-api2 2.9.0 (same import name), json-query monitors carry
  jsonPathOperator "==" (2.x keeps one without it DOWN), a fresh 2.x gets UPTIME_KUMA_DB_TYPE=sqlite
  (else it stops at a database-choice page), the first calls are retried while 2.x finishes its
  first start (they timed out once on a fresh volume). Update keeps kuma/data.v1 until the
  migration is done, heal.sh and the health gate leave a migrating Kuma alone, a rollback puts the
  1.x copy back.
- Hardcover (read-only checks with the owner's key, 2026-09-29): an ASIN finds the audiobook
  edition (B08G9PRS1K -> edition 31878554, book 427578), an ISBN or the exact title+author the
  book; the write mutations follow the live schema (introspection), not exercised on a real account.
- IRC (Shelfmark v1.4.0, read from its source): nothing here blocks it: DCC is outbound only
  (Shelfmark connects to the bot), ufw allows all outgoing, there are no egress rules. Its client
  sends no NickServ/SASL identification, and IRC Highway's #ebooks admits registered, identified
  nicks only (answered as "Timeout waiting for JOIN confirmation"), and bans automated clients.
  Read the cause: `docker logs shelfmark 2>&1 | grep -iE 'irc|dcc|join|welcome|offer'`. Unless
  that shows another cause, treat IRC as unsupported until Shelfmark learns to identify.
- On the VPS after Deploy:
  - Operations -> Update: answer Yes to "pins other images than the server runs" (CWA v4.0.8,
    Uptime Kuma 2.5.5-slim). Kuma migrates on its first start (seconds to minutes); Operations ->
    Monitoring afterwards shows the same monitors.
  - A Kobo: open its browser at https://request.<domain>/send, type the code on a book's page.
  - Devices: with a Hardcover token set, an audiobook you listen to appears on Hardcover within
    ~10 minutes (the Hardcover line on Devices says when it last sent).
  - Self-test: set HEALTH_PING_URL (Operations -> Monitoring) if it warns there is no external check.
  - Optional: Advanced settings -> lockout -> AUTHELIA_PASSKEYS=true, then register a passkey at
    https://auth.<domain> (Settings -> Two-Factor Authentication).

## Verified locally in v5.9 (chapters then the volume, comic safeguards, reading status, Metron)
- Unit 701 passed, installer 801 passed, end-to-end 290 checks / 0 failed. End to end: a volume
  file of 12 pages is held for the reader and leaves her dropbox; the intake check now waits for
  the page (it failed now and then when Calibre was a few seconds ahead of the portal's read).
- Measured (2026-09-29): the owner's indexers carry chapter releases for Chainsaw Man (9 of 42)
  and One Piece (14 of 316), none for Kagurabachi; MangaUpdates gives latest_chapter (Chainsaw
  Man 232); MangaDex links.mu is the MangaUpdates id in base 36 (75336092483 = ylx5wzn) and its
  aggregate maps volumes to chapters (vol. 19 = 165-175.5), untidy at times (vol. 20 = 176-203),
  so removal is always the reader's choice. "One.Piece.C1072.2023" was read as chapter 1072.2023:
  a chapter's decimal is now one digit.
- Found while testing, live since v5.8: a comic series page gave a 500 as soon as the reader had
  reading progress in it (its reading-direction value hid the reading-badge function).
- On the VPS after Deploy:
  - Comics: an open comic request now shows "confirm this copy" with the release; Yes to all per
    series. The menu shows Comics (n) while something waits.
  - A manga series: Follow chapters, then volumes (e.g. One Piece). The next chapter shows up
    under New for you; Request → confirm → it arrives as "One Piece (chapters)" #n.
  - A book on a Kindle: its page → Mark: Read. The series and AniList/Metron follow.
  - Devices → Connect Metron (user name + API key from your Metron profile).

## Verified locally in v5.8.3 (clickable follows, Get it for books with three safeguards, Hardcover retry)
- Unit 676 passed, installer 801 passed, end-to-end 288 checks / 0 failed. Book releases judged on 15 real-world spellings (scene
  names, "by", subtitles, series in brackets, initials): Dune Messiah is never Dune, a Stormlight
  1-5 pack, an M4B, a PDF, a summary and a French copy are refused.
- The safeguards, each tested: nothing downloads before the reader confirms (BOOK_CONFIRM=always,
  the default); 'Not it' blocks that release name from every indexer; a file whose inside says
  Dune Messiah, Brian Herbert or French is held, not imported, while an ISBN of an English edition
  (Hardcover's editions, measured 2026-09-29: isbn_13 / isbn_10 / language.code2) is accepted
  under any title; 'Wrong book' removes the owner tag and the wrong copy never counts as "already
  yours" again; a Shelfmark download that completed is never replaced by a second one.
- Found while testing: the admin's Requests page crashed when Shelfmark was down and the portal
  signs in with a password (its login was not guarded); it now says so on the page.
- The first Jeffrey Archer check on the VPS died on a ConnectTimeout to Hardcover (IPv4 fine, no
  IPv6 record): Hardcover is asked once more after 3 s with a 10 s connect timeout, a failed FIRST
  check is retried in 30 min (not 6 h), and Following shows the failure and the next try.
- On the VPS after Deploy:
  - Following: every name is a link. A book series or an author lists its books with Get it /
    Add to mine / Pick; a comic series opens its Comics page.
  - Get it on one book you do not have: within a minute or two Requests (with a count in the
    menu) shows the copy found; Yes, that one → downloading → checked → in your library.
    Shelfmark's queue shows it under the reader's name.
  - Not found after a few looks usually means the indexers name it differently: Pick in Shelfmark.
  - Optional: Advanced settings → requests → BOOK_CONFIRM=sure once the picks have proven right.

## Verified locally in v5.8.1 (Hardcover against the real API)
- With the owner's key: `{ me }` answers only as `Authorization: Bearer <hc_pat_...>` (the bare
  token is HTTP 400); search(Series|Author), series(...).book_series and books(where: ...) all
  answer. Measured and handled: search ranks a 4-book stray above Pratchett's 41-book Discworld
  (now sorted by size); a series holds untitled duplicate records (skipped); an author's books
  include 2035 placeholders, game supplements and split editions with 1-8 readers (author
  follows count released books with >= 5 readers). The installer's key check now strips a pasted
  "Bearer "/line break and shows Hardcover's own reason and the key's length when it refuses.
- whiptail under the POSIX locale printed a dash as "<80><94>": bookstack.sh uses C.UTF-8.

## Verified locally in v5.8 (following, New for you, reading status, AniList)
- Unit 628 passed, installer 792 passed, end-to-end 288 checks / 0 failed. End to end: alice's
  Kobo PUTs 'Finished' for her comic to the real Calibre-Web (as a Kobo does), and the portal reads
  it back as Read (book_read_link + kobo_bookmark) and as volume 3 finished for AniList.
- From the first night's alerts (2026-09-29): every self-test edge probe retries once when
  nothing answered (a single "/opds -> 000" flipped the hourly self-test red and back); the
  Cloudflare token check tries three times, and "no answer" (the network, not the token) is a
  plain notice only on two days running; the disk summary says MB under 1 GB and how much of
  Docker's images is old versions; ALERT_MAIL (default high) mails only problems, and Kuma's
  e-mail channel exists only with ALERT_MAIL=all (ntfy gets everything either way).
- Not verifiable here (no keys): Hardcover's series/author queries (Library -> Metadata sources
  now runs `python -m hardcover` after saving a key and shows the answer), AniList's OAuth and
  GraphQL (unit-tested against the documented shapes; connect once on Devices to prove it).
- On the VPS after Deploy:
  - Library -> Comics: the AniList client ID and secret (recreate the client first: its secret
    was in a screenshot). Then Devices -> Connect AniList as a reader.
  - Library -> Metadata sources: a Hardcover key, if books are to be followed; read the check line.
  - Follow a manga series and a book series; New for you fills when something comes out (the
    first check only records what is already out).

## Verified locally in v5.7 (comics and manga, audiobook downloads)
- Unit 612 passed, installer 790 passed, end-to-end 287 checks / 0 failed. KCC made the 12-page
  colour test volume's Kobo copy in 14 s on the Mac (a 200-page volume on the VPS: see below).
- End-to-end on the real containers: a manga volume dropped in alice's dropbox is matched to her
  request, arrives in Calibre as series "E2E Manga" #3 tagged Manga + owner:alice (the series read
  from the ComicBookInfo the portal wrote), KCC v12.0.0 adds a colour fixed-layout KEPUB to the
  same book, and Calibre-Web's real Kobo sync offers it to alice as EPUB3FL while bob's Kobo is
  not offered it. A Kindle copy (Colorsoft, KCC's Send-to-Kindle EPUB) is made under the mail
  limit. A broken CBR fails with its reason on the Status page.
- Measured: bsdtar cannot open solid RAR 3/4 archives ("RAR solid archive support unavailable"),
  unar can (rarfile's own RAR 3 and RAR 5 test archives, tests/fixtures/rar). unar costs ~205 MB in
  the portal image (GNUstep + ICU runtime), once.
- Read from source: Calibre-Web v4.0.7's Kobo sync prefers a stored KEPUB and sends pre-paginated
  books as EPUB3FL; its cover/metadata enforcer only rewrites .epub/.azw3 (so the .kepub is safe);
  Shelfmark v1.4.0 queues a release for another user with on_behalf_of_user_id (admin only) and
  searches Prowlarr category 7000, which includes Comics 7030.
- On the VPS after Deploy (things only the real box and devices can show):
  - Library -> Comics, then in Shelfmark: Settings -> Formats: tick CBR.
  - Request one manga volume (~200 pages) for a reader with a colour Kobo: watch
    `journalctl -t bookstack-comics` for the conversion time, and `docker stats` for KCC's peak
    memory against KCC_MEMORY (1536m). An out-of-memory conversion says so in the Kobo copy's
    failure note.
  - Sync the Kobo: the volume shows as a series book, in colour, pages turning right to left.
  - Send one to the Colorsoft: colour, and whether panel view works (Aa menu).

## Verified locally in v5.6.1 (self-test false alarms seen on the live server)
- Live 2026-09-28: "Tailscale IP not on any interface" while `ip -o addr show tailscale0` showed
  it: `ip -o addr | grep -q` under pipefail (SIGPIPE). Every long-output `| grep -q` in the
  scripts now searches captured output instead; tests/tui-test.sh fails if one comes back (779
  passed). Same fix in mem-tidy.sh, where it could have restarted Calibre-Web mid-import.
- One edge probe answered 000 (no reply in 12 s) while its neighbours answered 403: edge probes
  retry once on 000. btrfs/zfs report no inode table: an OK now, not a warning.

## Do on the VPS after Quick install
- [ ] `Operations → Self-test` is all green (it checks: containers, endpoints, Caddy +
      Authelia config, origin-pull CA, ufw posture, loopback-only binds, SSH key-only,
      `.env` 0600, every non-admin isolated, CWA registration off / Kobo on, public
      reachability through Cloudflare, origin refusing direct connections, backup timer).
- [ ] On NAT'd providers set `PUBLIC_IP` by hand in `.env` and answer No to "re-detect" in
      Configure; Caddy binds `BIND_IP` (the server's own source address) automatically.
- [ ] One real Kobo: Devices → generate link → device syncs only that user's books.
- [ ] One real Kindle: Library → Mail → test mail arrives; Devices → address; My books →
      Send to Kindle arrives (sender approved at Amazon).
- [ ] Shelfmark → Settings: choose release sources (nothing is on by default) and confirm a
      download lands in `library/dropbox/<username>/` and imports tagged.
- [ ] Library → Audiobookshelf ran (Quick install offers it); then one real audiobook upload
      shows "tagged owner:<user> in ABS" in its request detail.
- [ ] Cloudflare API token has **Firewall Services: Edit** (fail2ban bans at Cloudflare); after
      Security → fail2ban, `fail2ban-client status caddy-auth` shows the jail active.
- [ ] Library → Mail: test mail arrives; a request left pending mails the admin.
- [ ] Metadata: an hour after the first import, open a book's page (My books → the title);
      `journalctl -t bookstack-metapush` shows 'applied' lines, and a Kobo sync shows the title.
- [ ] Alerts: the test notification from Quick install → Alerts arrived on your phone.
- [ ] Monitoring: Deploy's summary says "N monitors at https://monitor.<domain>" with your alert
      channel named. Open it over Tailscale with the login under Operations → Monitoring; all
      green after a few minutes. `systemctl list-timers bookstack-selftest.timer` shows the next
      hourly run, and after an hour Self-test says "hourly self-test last finished N min ago".
- [ ] External check: a free healthchecks.io check (period 1 h, grace 1 h) entered under
      Operations → Monitoring; it turns green within the hour.
- [ ] Tailscale: key expiry disabled for this machine (the Tailscale step checks it).
- [ ] Cloudflare dashboard (inside the mfdata.in domain, not the account): WAF Managed rules ON on a paid plan only (Free shows "Upgrade plan": nothing to do); Bot Fight Mode **OFF** (it silently breaks
      Kobo, OPDS, KOReader and the Audiobookshelf apps and cannot be exempted on the Free plan).
- [ ] (Only if enabling Ephemera) `Operations → Ephemera` builds on the VPS; its success
      screen reports that Ephemera reaches FlareSolverr.
- [ ] (Only if Shelfmark's protected sources are used) `Operations → FlareSolverr` on; its
      success screen confirms Shelfmark reaches `flaresolverr:8191`.
- [ ] Seedbox (only if used): on the seedbox's Syncthing, this server is a remote device and
      each bookstack folder is **Send Only** (Full Rescan Interval 300); Library → Seedbox → Check
      says "every folder here is Receive Only", "connected" and "shared by the seedbox" for each
      folder; Shelfmark shows Torrent / NZB Completion Action as Keep / Copy; the path mappings and
      Completed Path Wait 3600 are set. Request one small book through Shelfmark: it arrives in the
      library, and the torrent is still seeding in ruTorrent afterwards.
- [ ] Family sharing (v5.3): after Deploy, `grep SHELFMARK_REQUESTS /srv/bookstack/.env` says
      `true`. As a reader, request in Shelfmark a book another reader already has: within
      seconds it closes with "Already in the family library", nothing reaches rTorrent/SABnzbd,
      and within a few minutes it is under that reader's My books. A MOBI-only book lands in
      the reader's library by itself (no needs-tag alert).
- [ ] Find a better copy (v5.4): on a badly converted book's page press *Find a better copy*,
      request it again in Shelfmark choosing an EPUB result; within minutes the page says it
      was replaced, the book keeps its owners, and the Kobo gets the new file at its next sync.
- [ ] Remove from my library (v5.5): a reader removes a shared book; it leaves their My books at
      once and their shelf in Calibre within minutes; the other reader keeps it. A book nobody
      has any more is deleted from the server after LIBRARY_RELEASE_DAYS (7).
- [ ] v5.5.1: `/etc/cron.d/bookstack-memtidy` exists (03:45); the next morning
      `journalctl -t bookstack-memtidy` shows one line per service. A day after an Update,
      `docker images` no longer lists the replaced Shelfmark/CWA versions (after the 7-day
      rollback window).
- [ ] v5, backups at home (free): Install -> Backups -> "A computer at home"; the home computer
      runs the printed `docker run`, the Tailscale rule is in place, and the step's own login
      check passed. First backup succeeds; `docker exec restic-rest ls /data/bookstack` on
      that computer shows the repository. Set a monthly reminder for the prune command.
- [ ] v5, backups (L15, bucket instead): the B2 key used by the nightly job has NO deleteFiles
      (`b2 key list` shows listBuckets,listFiles,readFiles,writeFiles only) and the bucket
      lifecycle keeps hidden files 30 days; Install -> Backups answered Yes to "append-only";
      Self-test says "backup key is append-only". Prune once by hand from your own computer
      (`RESTIC_PRUNE_ENV=./prune.env bash scripts/prune.sh`) or check
      `systemctl list-timers bookstack-prune.timer` if the prune key lives on the server.
- [ ] v5, origin lock (L14): token has SSL and Certificates: Edit; Security -> Origin lock ends
      with "Origin locked to THIS zone"; `curl -sI https://request.<domain>/healthz` still
      answers (not 525/526); Self-test says "only this zone's own Cloudflare client certificate".
- [ ] v5, one login (L05, only with Authelia on): after signing in at auth.<domain>,
      request.<domain>, books.<domain> and shelf.<domain> open without asking again, and
      audio.<domain> goes to Authelia and straight back signed in (it calls
      https://auth.<domain> from the server through Cloudflare: if it fails, look for a WAF
      event on /api/oidc/token); change a password on
      Devices, then sign out and back in at auth.<domain> with the NEW password within a minute
      (`journalctl -u bookstack-gate-sync -n 5` shows "password updated").
- [ ] v5, canary (L08): Operations -> Canary journey -> on; the first run passes; /admin shows
      the "Canary journey" card with an import time; Kuma shows "Canary journey" green.
- [ ] v5, torrents (L02, only if used): Library -> Torrents shows the Web UI login; it works at
      dl.<domain> with QBIT_PASS; Self-test says "qBittorrent Web UI accepts the stored admin
      password".
- [ ] v5, metadata: search a title on the portal, open its page, Request a verified copy; the
      book arrives with cover and description (`journalctl -t bookstack-metapush`).
- [ ] v5, isolation (L01): `docker exec shelfmark python3 -c "import socket;
      socket.create_connection(('calibre-web',8083),3)"` fails (Name or service not known).
- [ ] Plan conditions still hold (see "The plan" at the top): audiobooks < 40 GB, one shared
      FlareSolverr, batch jobs overnight.
- [ ] After the first week: `Operations → Backup restore test` green; `df /srv` < 70 % **and**
      `df -i /srv` < 70 %; no `OOMKilled` in `docker ps -a` / `dmesg`.

## Housekeeping
- The repo is under git (branch `main`, initial commit 2026-09-22). Tag the commit you deploy
  so the server copy can be diffed against a known state.
- `docs/DECISIONS-PENDING.md` records an unrelated lint hook on the dev machine that fails on
  every Python edit here; it does not affect the product.
