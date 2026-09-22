# Deployment checklist (pre-flight for the first VPS install)

Status as of 2026-09-22 (v4.3). Items marked **on the VPS** cannot be exercised on a laptop
and are covered by `Operations → Self-test` after deploy.

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
- [ ] Alerts: the test notification from Quick install → Alerts arrived on your phone.
- [ ] Tailscale: key expiry disabled for this machine (the Tailscale step checks it).
- [ ] Cloudflare dashboard: WAF Managed rules ON; Bot Fight Mode **OFF** (it silently breaks
      Kobo, OPDS, KOReader and the Audiobookshelf apps and cannot be exempted on the Free plan).
- [ ] (Only if enabling Ephemera) `Operations → Ephemera` builds on the VPS and RAM suffices.
- [ ] Plan: X8 (4c/8 GB/160 GB) recommended; on X4 keep audiobooks < 40 GB, Ephemera and
      Shelfmark browser sources off (`docs/RESEARCH-GAPS.md` §2 has the upgrade signals).
- [ ] After the first week: `Operations → Backup restore test` green; `df /srv` < 70 %;
      no `OOMKilled` in `docker ps -a` / `dmesg`.

## Housekeeping
- The repo is under git (branch `main`, initial commit 2026-09-22). Tag the commit you deploy
  so the server copy can be diffed against a known state.
- `docs/DECISIONS-PENDING.md` records an unrelated lint hook on the dev machine that fails on
  every Python edit here; it does not affect the product.
