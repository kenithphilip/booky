# Decisions pending / blockers

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
