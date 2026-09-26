# Decisions pending / blockers

## 2026-09-23 — Kobo has never received KEPUB, and the real enablement path is a trap

Kobo devices sync **EPUB** from this stack, not KEPUB, and always have. CWA v4.0.6
autodetects `kepubify` only at `/opt/kepubify/kepubify-linux-{64,32}bit`
(`cps/config_sql.py:568-579`), while the image installs it at `/usr/bin/kepubify`. So
`config_kepubifypath` is permanently empty and the conversion at `cps/kobo.py:281` never
fires. EPUB syncs and reads perfectly well on a Kobo; the only loss is that reading position
is recorded at chapter boundaries instead of continuously. Three places in this repo claimed
otherwise and have been corrected (`README.md`, `bookstack.sh`, `librarian/templates/devices.html`).

**Do NOT simply set the kepubify path.** Measured this round on the pinned image: with
`config_kepubifypath` filled in, the Kobo sync converts inline, on demand, one book at a time
while the device is waiting. The conversion's writers collide with the sync's reader and the
sync returns HTTP 500 **permanently** — 594 of 1,436 books were delivered and then nothing
until the container was restarted. Every green light stayed green throughout: Docker health,
CWA `/login`, portal `/healthz`, `_cwa_probe` and OPDS. The only symptom is a reader whose
device stops syncing.

The real enablement path, if KEPUB is ever wanted, is three steps in this order and no other:

1. Set `config_kepubifypath` to `/usr/bin/kepubify` (the path the image actually uses).
2. Run CWA's **Convert Library** job once, to completion, while **nobody is syncing**, so
   every KEPUB already exists on disk before any device asks for one. On 2 vCPU this is the
   most expensive job the box ever runs (a library conversion sustains 107.8 % CPU, i.e. more
   than one of the two cores), so run it overnight and expect the rest of the stack to be slow.
3. Only then let a device sync.

**Never let sync be the thing that converts.** If a book is added later, it is converted by
the same background job, not by the first Kobo that asks for it.

Note that the downstream half is already built and must not be removed: `librarian/library.py`
and `librarian/config.py` already serve a `kepub` format when one exists on disk, so once the
files are there the portal's Download and Devices pages pick them up with no further change.

**Decision needed from the owner:** leave Kobo on EPUB (recommended — it works, and the trap
above is a live foot-gun), or schedule the three steps above for a night when nobody reads.

## 2026-09-23 — BLOCKER: a foreign pre-commit gate refuses every `git add` here

A `PreToolUse` hook configured outside this repo,
`/Users/kenith.philip/grc-security-qna-service/.claude/hooks/gate-staged-code.sh`, blocks
`git add` in this project. It runs `uv run ruff`, which cannot start: `ruff` is not installed
on this machine and this repo is not a `uv` project, so there is nothing for it to resolve.

```
BLOCKED: staged Python does not pass the checks that execute.

RUFF:
error: Failed to spawn: `ruff`
  cause: No such file or directory (os error 2)


Fix, then stage again:
  uv run ruff check --fix src/ services/ tests/
  uv run ruff format src/ services/ tests/
```

The paths it names (`src/`, `services/`) do not exist here; they belong to the other project.
No file in this repo, and no hook, was modified to get around it: per the managed-settings
policy a gate that cannot be satisfied is reported, not bypassed.

**Effect:** the v4.3 work (the fan-out audit, all 77 verified findings, the synthetic-journey
fixes and the docs) is complete and green in the working tree but **cannot be committed**
until this is resolved. Verified before the block: portal suite 186 passed, installer harness
286 passed, end-to-end run on real containers 158 checks / 0 failed, ruff (isolated `uvx`)
clean over `librarian/` and `authelia/`.

**Decision needed from the owner**, any one of:
- scope that hook to the `grc-security-qna-service` project instead of user level, or
- install `ruff` on this machine so the gate can run (it would then lint this repo's Python,
  which already passes the same rules under `uvx ruff`), or
- run the commit yourself from a shell where the hook does not apply.

## 2026-09-22 — foreign lint hook fails on every Python edit (dependency missing)

A `PostToolUse` hook configured outside this repo,
`/Users/kenith.philip/grc-security-qna-service/.claude/hooks/lint-and-format.sh`, fires on
every `.py` write in this project. It runs `uv run ruff check <file>`; this project has no
`uv` project/lockfile and `ruff` is not installed, so it fails identically on each edit:

```
Lint issues in /Users/kenith.philip/booky/librarian/config.py — fix them now, not at the Stop gate:
error: Failed to spawn: `ruff`
  cause: No such file or directory (os error 2)
```

The hook's own header says it is "a prompt to fix, not a gate" and that the gate is
`verify-before-stop.sh` (from that other project). Nothing in this repo was modified to work
around it. The Python written here is linted independently with an isolated `ruff`
(`uvx --cache-dir "$TMPDIR/uv" ruff check librarian`) as part of the test run.

**Decision needed from the owner:** either scope that hook to the `grc-security-qna-service`
project (it is currently attached at user level), or add `ruff` to this machine / accept that
this repo is linted by its own test script instead.

## 2026-09-22 — upstream defect worth reporting: CWA's Kindle EPUB fixer rewrites CBZ files

`ingest_processor.py` (`add_book_to_library`) runs `EPUBFixer().process()` on every imported
file whenever the target format is EPUB and the fixer is enabled; only the fixer's CLI checks
for a `.epub` extension. On a CBZ it re-zips the entries and drops the archive comment, which
is the only place Calibre (9.1) reads ComicBookInfo metadata (title, tags) from. Reproduced on
`crocodilestick/calibre-web-automated:v4.0.6` with a standalone container. Our mitigation:
fixer off by default, fixes applied by the portal when mailing, honest `needs-tag` otherwise.
**Optional:** open an issue upstream with the reproduction in this note.

## 2026-09-22 — items deliberately left for after go-live

From `docs/RESEARCH-GAPS.md` (L01–L22): single sign-on *into* CWA/ABS (today Authelia gates,
the apps still show their own login), CWA's native per-user auto-send instead of the portal's
Kindle mail, pinned image digests with update notices, an alert channel beyond e-mail/webhook,
a scheduled canary user journey, and moving the portal off the host network. None blocks the
first deploy; each is a separate decision with its own cost noted in that file.

## 2026-09-22 — BookOrbit: admin-only trial, not a family rollout yet

Studied and run next to a read-only copy of the library (ghcr.io/bookorbit/bookorbit:3.0.0 +
Postgres). It left the library files untouched, read the `owner:<user>` subjects as genres,
and per-user genre filters isolated the web UI, direct links, OPDS and Kobo sync. Idle cost
about 300 MB for app + database.

Not rolled out to the family because: the project is 4.5 months old with one maintainer and
weekly releases (v3.0.0 the day before the study); a new user sees the whole library until
they own a book a filter can reference, and OIDC auto-provisioned users get no filter at all;
its login throttle is shared by everyone unless `TRUST_PROXY` is set; a Kobo syncs with only one
server (CWA or BookOrbit); it adds a Postgres to back up and more passwords; audiobooks would
lose their Audiobookshelf-side isolation; an open scanner bug (#1186) could attach one user's
file to another's book record.

**Decision needed from the owner:** if wanted, add it as an optional overlay (ebooks mounted
read-only, Tailscale-only at `read.<domain>`, pinned digest, `TRUST_PROXY` set, scripted
accounts with filters, pg_dump in backups) for a few weeks, then decide whether the family gets
it. The full study and the overlay design are in `docs/BOOKORBIT.md`.

## 2026-09-23 — RESOLVED: "Restore from backup" now restores the host files

`scripts/backup.sh` deliberately stages the host state that restic would otherwise never see
under `$STACK_DIR/.backup-snap/host/` — `/etc/ssh/sshd_config.d/01-bookstack.conf`,
`/etc/sysctl.d/90-bookstack.conf`, `/etc/docker/daemon.json`, the fail2ban jail and filters,
the `bookstack-*` cron and systemd units. `bookstack.sh`'s `step_restore` restores
`$STACK_DIR` (so those files come back as inert data under `.backup-snap/host/`) and then runs
`make_dirs; copy_code_trees; own_data_dirs; render_caddy_all; render_authelia_config;
step_cloudflare; install_backup_units; stack_up_all; render_fail2ban; install_disk_watch`.
**Not one of those reads `.backup-snap/host`.** fail2ban, the units and the cron files happen
to be regenerated from the checkout; `sshd_config.d`, `sysctl.d` and `/etc/docker/daemon.json`
are regenerated by nothing. So the documented disaster-recovery path onto a replacement VPS
produces a stack that is up and serving books while the SSH hardening, the kernel hardening
and dockerd's pinned default publish address are all absent — and the self-test, the restore
test and the restore's own success message all say the recovery worked.

`bookstack.sh` is not this agent's file. **What needs to change there:** after the MANIFEST
loop in `step_restore`, walk `$STACK_DIR/.backup-snap/host/etc`, list each file that differs
from what is live, and either copy it back (root-owned, original mode) or print the list and
tell the admin to re-run **Install → System** before trusting the box. The closing `msg`
should name the host files alongside the Kobo links, ABS accounts and Authelia logins it
already lists.

**FIXED 2026-09-23.** `bookstack.sh` gained `restore_host_files`, called from `step_restore`
immediately after `own_data_dirs` and before anything that regenerates config. It restores only
the three files nothing else regenerates — `sshd_config.d/01-bookstack.conf`,
`sysctl.d/90-bookstack.conf` and `/etc/docker/daemon.json` — and deliberately NOT the fail2ban
jail/filters, cron.d files or systemd units, which come back fresh from the checkout later in
the same function (copying the old server's versions over those would be a downgrade).
`/etc/bookstack/restic.env` is still never auto-restored: the repository credentials come from
the owner's password manager, by design.
Applied safely: `sysctl --system` runs immediately; the sshd drop-in is validated with
`sshd -t` BEFORE any reload and is REMOVED and reported if it fails, because locking the owner
out of a machine they are recovering is the worst possible moment for it; `daemon.json` is
restored but Docker is NOT restarted (that would kill the stack mid-restore) and the closing
message says so. Five assertions in `tests/tui-test.sh` cover all of it, including that the
regenerated files are not overwritten and that a failing drop-in is never reloaded.

Kept from before: `scripts/restore-test.sh` FAILs when
`sshd_config.d/01-bookstack.conf`, `/etc/docker/daemon.json` or `sysctl.d/90-bookstack.conf`
exist on the server but are missing from the snapshot, so at least the staging cannot rot
unnoticed while the restore gap is open.

## 2026-09-23 — restoring now requires the password manager, by design

`scripts/backup.sh` no longer copies `/etc/bookstack/restic.env` into the snapshot. It used to,
and because the staging directory lives inside the backup root with no matching `--exclude`,
every snapshot carried `RESTIC_PASSWORD` and — for an s3:/B2 repository —
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`: the key that decrypts the repository and the
credentials that can prune and delete it, stored inside the thing they protect. Anyone who
obtained the password once (an old snapshot restored on a laptop, a retired backup disk,
restore-test output on a shared machine) also held a bucket key with delete rights, and
rotating `RESTIC_PASSWORD` would not have revoked it.

A redacted stub goes in instead: `RESTIC_REPOSITORY` and `STACK_DIR` only, so a rebuild still
knows **which** repository to open.

**Operational consequence, and it is a real one:** restoring onto a replacement VPS now needs
the repository password and, for B2/S3, the application key, taken from the password manager.
`bookstack.sh`'s "Keep these OFF this server" screen should say so — it currently only tells
the admin to keep them off the server, not that a restore will ask for them back.
**Rotate once now**, since existing snapshots still contain the old values: `restic key add`
with a new password, *and* a new B2/S3 application key (the old one is in the old snapshots
regardless of the password change).

## 2026-09-23 — Shelfmark's cover proxy vs. the tailnet (COVERS_CACHE is the off switch)

`GET /api/covers/<id>?url=<base64url>` in Shelfmark is `@login_required` only, and on a cache
miss it fetches the caller-supplied URL. Its SSRF fence (`core/image_cache.py::
_prepare_safe_url`) rejects only `is_private / is_loopback / is_link_local / is_reserved`,
none of which covers `100.64.0.0/10`. Measured inside the pinned `v1.3.15` image (Python
3.14.7): `169.254.169.254` and `172.17.0.1` blocked, `100.64.1.5` **allowed**. Redirects are
re-checked, so the gap is the range, not the redirect handling. The portal's own fence
(`librarian/worker.py::_check_target`) does block that range — this stack already decided it
is in scope, and Shelfmark simply does not implement it. Caddy cannot help: it is outbound.

**Today's control** is the `tag:bookstack` tailnet ACL giving this node no outbound access to
other tailnet devices. `scripts/selftest.sh` now probes that from inside the shelfmark
container instead of assuming it (a connection that succeeds is a FAIL; one that does not is
reported as consistent-but-unproven, which is the honest reading).

**The off switch, if the ACL cannot be relied on:** turn `COVERS_CACHE_ENABLED` off in
Shelfmark's own Settings. That removes the endpoint entirely, at the cost of cover latency on
the search page. **Re-check `_prepare_safe_url` whenever `IMG_SHELFMARK` is bumped** — v1.3.15
did not add the CGNAT range, and a later release may.

## 2026-09-23 — two removals recommended to the owner, NOT done (they cross file ownership)

**RESOLVED 2026-09-25 — the owner decided to KEEP BOTH, and to finish them instead.** Do not
recommend these removals again. What was done (v4.6):
- *Uptime Kuma* is configured by `monitoring/kuma_bootstrap.py` after every Deploy (monitors,
  the alert channels alert.sh uses, push monitors for every scheduled job, reboot maintenance
  window), so the "dashboard with nothing on it" in (b) no longer exists. The hourly self-test
  timer that (b) proposed as Kuma's replacement was built as well, and reports INTO Kuma.
  (b)'s claim that the replacement was "already built" was wrong at the time: the self-test only
  ran unattended once a day, after the reboot.
- *Ephemera* stays, with FlareSolverr now one shared service that Shelfmark's protection-
  challenge solving uses too. (a)'s "needs >= 8 GB" was a desk estimate; measured, Ephemera +
  FlareSolverr put the worst case at ~3.3 GB on the 4 GB box. What (a) got right still holds:
  upstream is gone, the build is a pinned third-party re-upload, and Ephemera has no accounts.
  Its unique value, which (a) did not weigh, is the request-and-wait queue: neither the portal
  nor Shelfmark keeps looking for a title that is not available yet.

The original analysis follows, unchanged, for the record.

Both are arguments for deleting something, both look right, and neither was carried out this
round because each needs a matching change in `bookstack.sh` and `tests/tui-test.sh`, which
this agent does not own. Doing half of either is worse than doing neither — Deploy would
render a Caddyfile for a compose overlay that no longer exists, or a menu entry would start a
container that is gone. They are recorded here so the decision is the owner's and the evidence
does not have to be gathered again.

**(a) Remove Ephemera and FlareSolverr.** The overlay's own header already says it needs
≥ 8 GB (FlareSolverr's headless Chromium is fenced at 1 GB on its own) and that upstream —
project and image both — was removed from GitHub in early 2026, so no security fixes are
coming. The box is confirmed 2 vCPU / 4 GB, and `docs/RESEARCH-GAPS.md` §9 already rejects
running it here. The build pulls a third-party fork
(`github.com/Nsoromma/ephemera-book-downloader.git#e98e994…`) and compiles a Node/pnpm/tsc/vite
application on the VPS, which the same docs put at 1–2 GB of build RAM. It is switched off,
cannot be switched on on this hardware, and still costs: a compose overlay, an `ephemera.`
site block in `caddy/Caddyfile.template`, an `EPHEMERA_ENABLED` branch in
`scripts/selftest.sh`, 52 references in `bookstack.sh`, 20 assertions in `tests/tui-test.sh`,
13 in `README.md`, and one of the three axes that make the Caddyfile validation matrix eight
variants. Removing it halves that matrix to four and deletes an unreviewable supply-chain path.
**Nothing is lost**: Shelfmark covers the same search-and-download job with real per-user
accounts and per-user dropboxes, which Ephemera never had — that is exactly why it sits behind
the admin password gate on the tailnet today. If the owner says yes, both halves go in one
round: **C** deletes `docker-compose.ephemera.yml`, the `@EPHEMERA_BEGIN@…@EPHEMERA_END@`
block, `EPHEMERA_ENABLED` and `IMG_FLARESOLVERR` from `.env.example`, the selftest branch and
the README/docs sections; **A** deletes `step_ephemera_on`/`step_ephemera_off`, the menu
entries and the ~20 TUI assertions, and drops the render test from 8 variants to 4. The pinned
fork commit is recorded above so the decision is reversible.

**(b) Remove Uptime Kuma.** Nothing anywhere bootstraps a monitor: `step_monitoring` starts
the container, waits for port 3001 and then displays a to-do list telling the admin to open a
web UI over Tailscale and hand-create seven monitors plus a notification channel. There is no
Kuma API or socket.io call anywhere in `bookstack.sh` or `scripts/`. The same dialog admits
the structural defect — Kuma runs *on* this server, so it cannot report the VPS being down,
which is the failure worth paying for. Meanwhile `scripts/selftest.sh` already checks every
endpoint those seven monitors would (portal `/healthz`, shelfmark `/api/health` and its auth
mode, CWA `/login`, ABS `/healthcheck`) plus a long list Kuma structurally cannot: OOMKilled
containers, per-user tag isolation in CWA *and* ABS, the library's own owner-tag invariant,
orphan dropboxes, ufw posture, effective `sshd -T`, loopback-only binds, Caddy admin off TCP,
origin refusing direct connections, `cf-cache-status`, Kobo `/v1/initialization` not being
challenged, restic snapshot age and last exit status. `scripts/alert.sh` is already the
delivery channel. Cost today: 384 MB of `mem_limit` on a 4 GB box, `kuma/data/kuma.db` in the
nightly SQLite snapshot loop, `kuma/data` in `RESTORE_CONFIG_PATHS`, `IMG_KUMA` in the Update
list, `uptime-kuma` in the selftest core-container list, the restart menu and the early-start
list — in exchange for a dashboard with nothing on it. And if the seven monitors ever *were*
configured as instructed, four of them point at `https://books.$d` etc., i.e. out to
Cloudflare and back in through the mTLS origin-pull handshake: roughly 10k self-inflicted
round trips a day on two cores.
**What replaces it is already built:** give the post-reboot self-test unit a `.timer` as well
(hourly is plenty) so `selftest.sh` runs unattended and a non-zero exit reaches `alert.sh`;
`HEALTH_PING_URL` (added to `.env.example` and pinged by `selftest.sh` this round) is the
external dead-man's switch that covers the one thing Kuma never could.
**What is genuinely lost, stated plainly rather than sold as a pure win:** uptime history
graphs and a status page. For four readers with a whiptail console over SSH that is not worth
384 MB and six code sites — but it is a real loss, and the owner should decide it knowing that.
