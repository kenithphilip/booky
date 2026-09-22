# Decisions pending / blockers

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
