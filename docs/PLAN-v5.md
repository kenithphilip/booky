# v5 plan — everything still open, in build order

Owner's instruction (2026-09-26): fix ALL pending items, ALL backlog entries, ALL stubs and
anything not fully wired. The metadata engine (search, fetch, library enrichment) comes first,
built on the free sources, taking its design from Readarr / Shelfmark / CWA / Ephemera rather
than inventing one. Each item is closed only with a test that exercises it; its Status line in
`docs/RESEARCH-GAPS.md` changes in the same round (the Backlog-truth probes enforce that).

## What the research established (2026-09-26, source read, not guessed)

- **Open Library search is the link we were missing.** `search.json` returns, per work, the
  Gutenberg ids, LibriVox ids, Standard Ebooks slug, Internet Archive identifiers, ISBNs, author
  key, cover id, languages and `ebook_access` (`public` = downloadable). A book found through
  metadata can be resolved DIRECTLY to downloadable copies in our catalogs — no keyword guessing.
  Fast (well under a second), keyless.
- **bookinfo.pro** (the Goodreads mirror Readarr used): `/search` answers ids only; `/work/{id}` and
  `/author/{id}` hydrate them (0.4 s warm, 14–53 s cold). Rich: series, ratings, author photos and
  bios, editions with ISBN/language/format. Background use only, never on a page's critical path.
- **Google Books**: keyless quota is shared and was exhausted (HTTP 429) — usable only with the
  owner's free API key (optional `GOOGLE_BOOKS_API_KEY`).
- **Readarr**: metadata-first; release query ladder (author+title → title → "title author" →
  title only, stop at the first tier with hits); fuzzy matching (Levenshtein + token score,
  title > 0.7, author > 0.8); import identification by weighted distance (ISBN 10, language 5,
  wrong format 5, author 3, title 3, year 1, publisher 0.5; missing-id 0.1) auto-accepted at
  distance ≤ 0.20, otherwise manual review with the penalty names as reasons; per-source
  back-off 1 m → 24 h; blocklist + re-search on a failed download.
- **CWA**: metadata providers only for editing books already in the library; its endpoints need a
  session plus a scraped CSRF token (brittle). Its Hardcover confidence scorer and title-token
  normalisation are worth copying; its auto-fetch stays OFF (tag append + "latest book" fallback).
- **Ephemera**: no metadata at all; its "requests" take `results[0]` of a saved query with no
  matching. Its API has no authentication. Useful as a *source* behind our own matching only.
- **Shelfmark**: see phase 1 — its native metadata providers decide how much we build.

## Phase 1 — metadata engine (search, fetch, enrichment)
1. `bookmeta` search: Open Library search as the fast primary; results are WORKS (cover, author,
   year, series when known, "in your library" / "available as ebook / audiobook" badges).
2. Work page `/work/<ol-key>`: description, author, series, editions, availability per catalog
   (resolved from the cross-links), "Get it" per verified copy, "Keep looking" when none.
3. Author page for any author (`/writer/<ol-key>`): bio, photo (through the local cover proxy),
   works with library/availability badges. Series pages from bookinfo when it is warm.
4. Fetch: candidates come from the cross-links first, then the catalog keyword ladder; every
   candidate is scored with a Readarr-style weighted distance against the chosen work and
   carries its reasons; ≤ 0.20 → requestable, else "check this one" with the reasons.
5. Requests carry the work id, the candidate's evidence (expected size / md5 / sha1, source ids)
   via a server-side candidate cache — the form posts a token, never URLs (fixes the Internet
   Archive verification that never fired, and closes the tampering path by construction).
6. Edition gating: language (per-reader preference), abridged / adapted / excerpt, omnibus —
   from metadata + title evidence; a mismatch is never automatic.
7. Keep looking re-targeted at works: rechecks resolve through Open Library cross-links (new
   Gutenberg/LibriVox/SE/IA copies appear there), then the ladder; ISBN/OL-work evidence now
   actually available to verify with.
8. Enrichment: Open Library work/author records become first-class in the metadata store; bookinfo
   fills series/ratings/bios in the background; Google Books when a key is set.
9. Stage A remainder: Gutenberg DCMIType + language, LibriVox totaltimesecs / num_sections /
   language, OPDS dc:identifier / length; every rejected candidate records its reason.
10. `.opf` sidecars read (identification) before they are discarded.

## Phase 2 — library correctness
- L10 owner reconciliation for mobi/azw3/fb2/txt/djvu (through the host push path: the portal
  still never writes metadata.db; a narrow "add owner tag to an untagged book" job, verified
  before and after).
- L11 real KEPUB (pinned kepubify, cached conversion) or remove the option.
- L21 asynchronous Send-to-Kindle.
- L04 re-evaluated: CWA auto-send shares the "latest book" fallback — likely *dropped* with the
  evidence, the portal's post-import send kept.

## Phase 3 — acquisition & approvals
- L16 Shelfmark request policies + its requests in the portal's Pending card (API verified first).
- L02 qBittorrent credentials, temp path, dropbox-only placement, selftest login.
- L18 large uploads over the tailnet (`upload.` vhost, Tailscale-only).

## Phase 4 — security
- L01 container isolation (portal off host network, named networks, cap_drop, read_only where it
  holds, Shelfmark mounts only app.db, `books` user out of the docker group).
- L05 single sign-on with Authelia (header SSO, header stripped everywhere, password sync).
- L14 per-zone authenticated origin pull certificate (or Tunnel), README wording corrected.
- L15 append-only backup key + separate prune credentials.
- L17 Turnstile on the portal login; 2FA nudge in Quick install; selftest warning.
- L19 remaining host hardening (sshd extras, sysctl, Tailscale apt repo, cf-ips port 80, journald).

## Phase 5 — operations
- L06 update notices (registry check timer → alert; CWA banner setting).
- L08 synthetic canary journey, twice daily, results on /admin.
- L09 certificate / origin CA / Cloudflare token expiry watch.
- L12 Kobo card: prerequisites, last sync, test link, shelves-only / Hardcover token setters.
- L20 autoheal (decision recorded: socket is root-equivalent).
- TUI: keep-looking list (admin_cli + menu), metadata health.

Every phase ends with: portal, installer, monitoring and end-to-end suites green; ruff and
shellcheck clean; docs + Status lines updated.

## Progress (2026-09-26)
All five phases are implemented. Each item's Status line in `docs/RESEARCH-GAPS.md` is checked
against its code by `tests/tui-test.sh`. Where the result differs from the plan above:
- **L01**: done without two parts of the original "How":
  - The portal stays on the host network. It reaches every service on its 127.0.0.1 port, and
    nothing on a bridge network can reach the host's loopback.
  - `read_only` is not used: the s6/gosu images write to /run and /etc at start.
  - "Shelfmark mounts only app.db" was dropped. app.db itself holds the hashes, Kobo tokens and
    SMTP password, and a single-file bind of a WAL database hides new users from Shelfmark.
- **L04 and L13**: dropped, with evidence. CWA's "latest book" fallback could send or rewrite
  the wrong reader's book.
- **L05**: one login covers the portal, Calibre-Web and Shelfmark (header login; admin from the
  Authelia `admins` group, which mirrors Calibre-Web's admins), and Audiobookshelf's web page
  (OpenID Connect through Authelia; its apps keep their local login).
- **L15**: adds a free append-only target, restic's rest-server on a computer at home over
  Tailscale.
- **L14**: the per-zone certificate option was implemented. Cloudflare Tunnel stays documented
  as the stronger alternative.
- **L03 and L22**: obsolete (the P2P path and aria2 are gone).
- **L02, L06, L07, L08, L09, L10, L11, L12, L15, L16, L17, L18, L19, L20, L21**: done.

Proven against stubs only (the checklist's VPS section is the real proof): L14 (the Cloudflare
API) and L15 (restic against a real B2 bucket).
