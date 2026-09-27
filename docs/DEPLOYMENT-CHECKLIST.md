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
- [ ] Cloudflare dashboard: WAF Managed rules ON; Bot Fight Mode **OFF** (it silently breaks
      Kobo, OPDS, KOReader and the Audiobookshelf apps and cannot be exempted on the Free plan).
- [ ] (Only if enabling Ephemera) `Operations → Ephemera` builds on the VPS; its success
      screen reports that Ephemera reaches FlareSolverr.
- [ ] (Only if Shelfmark's protected sources are used) `Operations → FlareSolverr` on; its
      success screen confirms Shelfmark reaches `flaresolverr:8191`.
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
