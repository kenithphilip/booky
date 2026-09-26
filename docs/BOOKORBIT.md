# BookOrbit as a second reader next to CWA

Study and hands-on trial from 2026-09-22 (BookOrbit v3.0.0). Nothing here is deployed; it is
the design to follow if the owner decides to trial it (see `DECISIONS-PENDING.md`).

## What it is

BookOrbit is a self-hosted library and reading server built with NestJS/Fastify on Node 26 and a web client, backed by Postgres with pgvector. It handles ebooks (EPUB, PDF, MOBI/AZW3, FB2), comics (CBZ/CBR/CB7), audiobooks (M4B/MP3/FLAC…) and podcasts, and has web readers and players, Kobo sync, KOReader sync with its own plugin, OPDS, send-by-email/Kindle, 9-14 metadata providers, Hardcover/Readwise/StoryGraph sync, reading statistics and achievements, and OIDC. It also has features that overlap this stack: a "Book Dock" drop folder, uploads, and a "book request" module with download clients and indexers, which is a Shelfmark equivalent. License: AGPL-3.0-only. Since 10 Sep 2026 it also carries ADDITIONAL_TERMS.md, which requires attribution in the UI; that does not matter for private, unmodified use. Sources: https://github.com/bookorbit/bookorbit, https://bookorbit.app/, and the v3.0.0 source tarball (ADDITIONAL_TERMS.md, Dockerfile, docker-compose.yml).

## Version and image

Current release is v3.0.0, published 2026-09-21T07:49Z, one day before this audit (GitHub releases API). Earlier releases: v2.10.0 (09-14), v2.9.0 (09-07), v2.8.1 (08-29), and v2.0.1 through v2.7.0 all re-published on 08-23. Image: ghcr.io/bookorbit/bookorbit:3.0.0. I pulled it; its digest is sha256:571ea47b036a8b371db9c54d54a3cea0fb96245f66c09584efac799afd339716, it is multi-arch (arm64 pulled here) and 1.2 GB. The upstream compose defaults to :latest, and .env.example says to pin a sha tag or digest. It needs pgvector/pgvector:pg18 (digest sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a, 648 MB) and optionally kokoro-fastapi for TTS. Required environment: APP_URL, POSTGRES_*, JWT_SECRET, PODCAST_ENCRYPTION_KEY, SETUP_BOOTSTRAP_TOKEN. Optional: PUID/PGID (default 1000), TRUST_PROXY, LIBRARY_BROWSE_ROOT, DISABLE_LOCAL_AUTH, and NODE_MAX_OLD_SPACE_SIZE=auto (65% of the cgroup limit when under 2 GB, per server/entrypoint.sh). DB migrations run automatically at every start (entrypoint.sh: `node dist/scripts/migrate.js`). Sources: https://bookorbit.app/installation, and in the repo docker-compose.yml, .env.example, server/entrypoint.sh.

## Maturity

The project is very young, releases very fast and depends on one person. The repo was created 2026-05-09, so it is about 4.5 months old. It has 4,583 stars, 296 forks and 441 open issues. The last push was 2026-09-22 14:53Z, with 5 fix commits that day. The contributor list shows neonsolstice with 1,069 commits, dependabot 47, then 14, 4, 4, 2… so the bus factor is effectively 1. Cadence is a minor release every week and a major version (v3) yesterday. The license terms changed 2026-09-10. The repo ships CLAUDE.md/AGENTS.md and an AI policy.

Open bugs that matter here:
- #1186/#1216: "Scanner reassigns files to the wrong book when a deleted file's inode is reused". CWA rewrites and replaces files, so this is relevant.
- #1175: Kobo sync fails on an incompatible x-kobo-synctoken.
- #1323/#965: Calibre custom columns are not imported.
- #1397: empty folders are not cleaned up.

Code quality looks serious: strict CSP, a read-only container, cap_drop, throttled auth endpoints, and content filtering enforced centrally (book.service.ts:523-539, book-query-builder.service.ts:87). But the pace and the single maintainer are a real risk for a year of unattended running. Sources: GitHub API (repos/bookorbit/bookorbit, /releases, /contributors, /commits, search/issues), https://github.com/bookorbit/bookorbit/issues/1186, /1216, /1175, /1323.

## Storage model

BookOrbit indexes folders in place and keeps its catalogue in Postgres, with covers, the book-bucket and the Book Dock under /data.

- It works with a read-only library mount. Hands-on, I mounted a Calibre-style tree read-only at /books/calibre. It scanned fine, and SHA1 checksums of all 10 original files plus the directory list were identical before and after. It wrote nothing into the library.
- The entrypoint only chowns /data and /tmp, never /books (server/entrypoint.sh, fix_owner / is_managed_data_path).
- Write-back into files is off by default. The library master switch is `fileWriteEnabled` default false (server/src/db/schema/libraries.ts:55), and the per-format flags only apply when it is on. File renaming is off by default (fileRenameEnabled, libraries.ts:65). Auto metadata fetch on import is off by default (book-metadata-fetch-config.service.ts:20, triggerOnImport:false).
- It is not a Calibre-DB reader. metadata.db is ignored. Each book folder (default organizationMode 'book_per_folder') is read, and metadata comes in precedence order folderStructure, embedded, nfoFile, opfFile (Calibre's metadata.opf), sidecar (libraries.ts:37-41, scanner.service.ts:1798). CWA's `Author/Title (id)/` layout maps cleanly: titles came out right in my test.
- EPUB/OPF dc:subject becomes BookOrbit **genres**, not tags (server/src/modules/metadata/lib/opf-parser.ts:483). Observed: 'owner:alice', 'owner:alice,owner:bob' and 'owner:alice,Fiction' were stored as genres, and an MP3's ID3 genre also became a genre.
- New books are only picked up by a watcher (off by default, libraries.ts:31) or a cron scan. Creating a library triggers a first scan.
- Folder role is 'downloads', which a code comment describes as BookOrbit-owned ("names files, evicts them under quota pressure", libraries.ts:93-99). In practice that applies to podcasts, and with a :ro mount any write attempt fails harmlessly.
- Uploads, Book Dock import, requests/downloads, delete and bulk-rename would all try to write into the library. With :ro they fail. That is desirable: CWA stays the single writer.
- Database: Postgres 18 with the pgvector, pg_trgm and uuid-ossp extensions (.env.example). SQLite is not supported.

Sources: https://bookorbit.app/library-file-structure/, https://bookorbit.app/installation, and the source files cited.

## Multi-user and isolation

BookOrbit can enforce per-user isolation, but only as its own filter layer, and I verified it hands-on.

**Model**
- Users have granular permissions (packages/types/src/permissions.ts: library_download, kobo_sync, koreader_sync, opds_access, email_send, library_upload, library_edit_metadata, manage_*, book_request_*…).
- Access is granted per library (user_library_access, schema/auth.ts:94).
- Per-user content filters can include or exclude by tag or genre (user_content_filter_tags/genres, auth.ts:214-241; content-filter.repository.ts).
- Superusers bypass the filters (book.service.ts:523).
- Collections are per user.

**Hands-on test.** I created alice and bob (non-admin, access to both libraries) and set includeGenreIds to owner:alice and owner:bob respectively.
- Alice listed exactly Pride and Prejudice, Moby Dick and the Emma audiobook. Bob listed Moby Dick and Tom Sawyer. Admin saw all 5.
- Direct GET /api/v1/books/{id} returned 404 for another user's books (alice: 1→404, 5→404; bob: 1,2,4→404).
- Alice's OPDS feed (/api/v1/opds/catalog, HTTP Basic with an OPDS sub-account) listed only her 3 titles.
- Alice's Kobo sync with a 'Sync to Kobo' collection returned only her 2 EPUBs. Her attempt to add book 5 (Bob's) and book 1 (Carol's) to her own collection was silently filtered out (bookCount 2).

So the owner:<user> dc:subject tags the librarian embeds (librarian/tagger.py:3,34) carry over into BookOrbit with no extra work.

**Gaps**
1. **New users see everything at first.** A filter can only reference a genre id that already exists (content-filter.repository.ts validateEntityIds, around line 150). A new user with no owner:<user> book yet cannot be filtered, and a non-superuser with no filter sees the whole library. The same applies to OIDC auto-provisioned users: they get the default permissions and libraries but no filter (oidc.service.ts around 345-355; user.service.ts:465-474 default library access).
2. **Sharing done in CWA does not carry over.** If an admin changes Calibre tags in CWA (for example adds owner:bob to share a book), only metadata.db and possibly the OPF change. BookOrbit reads the embedded EPUB subjects first, so the change does not propagate. It must be repeated in BookOrbit. This is my reading of the precedence order, not tested.
3. **Possible cross-user leak.** Open scanner bug #1186 (a file reassigned to the wrong book on inode reuse) could put one user's file under another user's book record. Plausible with CWA replacing files; not reproduced.
4. **Audiobooks.** ABS isolation lives in the ABS database (librarian/abs.py:6-9; worker.py:455), not in file tags. BookOrbit would see pipeline audiobooks with no owner genre, so they would be invisible to filtered users.

Sources: https://bookorbit.app/ ("Per-user libraries with permissions"), https://github.com/bookorbit/bookorbit (README: "Granular per-user permissions and isolated reading data"), and the source files cited.

## Sign-in and SSO

**Local accounts**
- The setup endpoint needs the x-setup-token header.
- POST /api/v1/users takes username, name, email (required, unique, must be a valid email), permissionNames and libraryIds, sets a random password, and returns a one-time resetUrl (user.service.ts:134-166). Verified: POST /api/v1/auth/reset-password {token,newPassword} then works. This is fully scriptable from bookstack.sh.
- OPDS and KOReader use separate per-user sub-accounts (opds_users / koreader_users tables linked to user_id; schema/opds.ts:16-32, schema/koreader.ts:23-31).
- Kobo uses a per-device 32-hex token (kobo-device.service.ts:30).

**OIDC**
- Any provider works; docs say Authelia was tested and works as a public client. Redirect URI is https://<host>/oauth2-callback.
- It can auto-provision users, map groups to permissions on every login, and link local accounts.
- DISABLE_LOCAL_AUTH=true removes password login, with a lockout guard.
- Using Authelia as the provider would require adding an identity_providers.oidc block (HMAC secret, JWKS key, client) to authelia/configuration.yml.template, which today is a file backend plus forward-auth only (configuration.yml.template:1-40). And auto-provision creates users without the owner filter (see isolation).

**Forward-auth headers:** not supported as identity. No Remote-User or trusted-header code exists in server/src (grep found none). The auth-proxy doc only covers putting a gate in front and bypassing device paths. BookOrbit still needs its own login behind Authelia (a double login).

**Throttling (important)**
- Login is limited to 5 per minute per IP (auth.controller.ts:83), and setup/register to 3 per minute.
- Client IP comes from Fastify trustProxy, which is only set from TRUST_PROXY (main.ts:35), unset by default.
- Hands-on, the log showed `ip=192.168.117.1`, the Docker gateway, for every login. Behind Caddy, all family members plus any internet attacker would share one bucket. I hit 429 myself after 5 logins.

Sources: https://bookorbit.app/oidc/, https://bookorbit.app/auth-proxies/, and the source files cited.

## Kobo, KOReader, OPDS

**Kobo**
- Each paired device gets a sync URL https://<host>/api/v1/kobo/{deviceToken}, shown once. BookOrbit keeps a per-user snapshot and sends deltas. It syncs progress, highlights, notes, ratings and reading sessions, uses KEPUB conversion (cached), and proxies unhandled calls to the Kobo store.
- Only EPUBs in collections with 'Sync to Kobo' enabled reach the device; collections become Kobo tags. Verified: sync returned [] until I made such a collection, then 2 NewEntitlements.
- Highlights use /api/v3/* and /api/UserStorage/*, which sit outside the global prefix (main.ts:67-69, which also excludes api/kobo/:deviceToken/*).
- A Kobo's api_endpoint can point at only ONE server: CWA's books.<domain>/kobo/<token> (managed today by the Users menu and the portal via librarian/cwa.py) or BookOrbit's. Switching a device means editing Kobo eReader.conf and losing CWA shelf-based sync. Choosing BookOrbit also means each reader has to curate a 'Sync to Kobo' collection. CWA syncs all visible books or selected shelves.

**KOReader:** it has its own plugin (catalog browser, page-stat events) and progress sync via KOReader sub-accounts. Again one sync server per device, and it overlaps the librarian's optional kosync (docker-compose.yml:154) and CWA's /kosync.

**OPDS**
- Root is exactly /api/v1/opds. Docs warn that a `/api/v1/opds/*` bypass leaves the root empty.
- HTTP Basic with realm "bookorbit OPDS". Verified: 401 when unauthenticated, and the catalog is filtered per user.

**Paths to bypass an auth gate:** /api/v1/opds*, /api/v1/koreader*, /api/v1/kobo/*, /api/v3/*, /api/UserStorage/*. Check with curl: a gated path returns 302, a correctly bypassed one returns 401 with www-authenticate basic.

Sources: https://bookorbit.app/kobo/, https://bookorbit.app/koreader/, https://bookorbit.app/auth-proxies/.

## Audiobooks vs Audiobookshelf

BookOrbit plays audiobooks: single-file or multi-track in one folder, with disc folders flattened. It needs one folder per audiobook, and any subfolder with audio becomes a book. It also does podcasts and optional Kokoro TTS read-aloud (https://bookorbit.app/library-file-structure/; docker-compose.yml kokoro profile). Hands-on, it picked up a test MP3 in `Author/Title/` and read its ID3 genre.

For this stack it duplicates Audiobookshelf 2.36.1, and it would lose isolation. ABS isolation is ABS-database tag restriction set by the librarian through the ABS API (librarian/abs.py:6-9, worker.py:455; README.md:166-169). The files carry no owner tag, so in BookOrbit those audiobooks would either be hidden from filtered users (include filter) or visible to all (unfiltered users). The ABS mobile apps, whose bypass paths are already wired (inject-gate.py:22), are mature. Recommendation: do NOT mount ./library/audiobooks or ./library/podcasts into BookOrbit. Keep ABS as the only audio front end.

## Resources

Measured with docker stats on OrbStack arm64, app mem_limit 768m (auto heap about 500 MB), Postgres limit 256m:
- **Just after first start:** app 311 MiB, Postgres 120 MiB.
- **Idle after setup:** app 164-208 MiB, Postgres 78-98 MiB.
- **Scanning 500 books** (identical EPUBs with Calibre OPFs, about 1 KB each): 85 s, app peaked at about 224 MiB with CPU 34-45% of one core. Scanning 4 books took 12 s (fixed overhead).
- **Upstream minimum:** 1 core and 512 MB free, with 1 GB+ recommended (https://bookorbit.app/installation).
- **Disk:** about 1.9 GB of images (1.2 GB app plus 648 MB Postgres), plus DB and covers.

**Budget:** roughly 300-450 MiB resident for app plus DB, with fences of 768m + 256m ≈ 1 GiB.
- **X4 (2c/4 GB/80 GB) — the deployed plan.** Corrected against the round-4 measurement on the real stack (README.md, "VPS sizing"): the core stack is **≈ 1.2 GB idle and ≈ 2.2 GB peak** on the 4 GB box, not the 1.3-1.7 GB resident *plus* unbounded spikes this paragraph used to assume. So the RAM objection is weaker than it was written: about 0.4 GB resident / 1 GB fence fits in the ~1.8 GB of headroom at peak. Two objections survive and they are the ones that matter on this plan: **about 2 GB of the 80 GB disk**, which is the binding constraint, and **CPU** — a 500-book scan already costs 34-45% of one core for 85 s, and there are only two cores, one of which a CWA library conversion can occupy on its own (107.8% measured). It remains in the same class as the Ephemera overlay, which the checklist keeps off.
- **X8 (4c/8 GB/160 GB):** comfortable, but the reason to move is disk, not memory.

## Hands-on trial (2026-09-22)

**What I ran** (all with dangerouslyDisableSandbox, all containers prefixed bo-audit-):
- Pulled ghcr.io/bookorbit/bookorbit:3.0.0 (digest above) and pgvector/pgvector:pg18.
- Created network bo-audit-net and volume bo-audit-pg, and ran bo-audit-db (256m).
- Ran bo-audit-app on 127.0.0.1:38300 with upstream hardening (read_only, tmpfs /tmp, cap_drop ALL plus the 5 caps, no-new-privileges, 768m).
- Mounted a scratch Calibre-style tree READ-ONLY at /books/calibre: 4 EPUBs with dc:subject owner:alice / owner:bob / owner:alice+owner:bob / owner:carol, 3 with metadata.opf, a dummy metadata.db, and metadata_db_prefs_backup.json. Also mounted a test audiobook dir (MP3 with ID3 genre owner:alice) read-only at /books/audiobooks.

**Observations**
1. Healthy in about 6 s; migrations and seeding at startup. It checks GitHub for updates at boot (AppInfoService; can be turned off in app settings, app-info.service.ts:24).
2. Setup via x-setup-token worked; libraries were created via API and scanned automatically.
3. Titles and authors parsed correctly; owner tags stored as genres.
4. Library files and directories unchanged: SHA1 of all original files identical after all tests, no new dirs.
5. Per-user genre include filters isolated web lists, direct book GETs, OPDS and Kobo sync (details under multi-user).
6. The Kobo endpoint returns 401 for a bad token and [] until a Sync-to-Kobo collection exists.
7. The login throttle tripped (429) after 5 logins. All requests appeared from the Docker gateway 192.168.117.1 because TRUST_PROXY was unset.
8. 500-book scan took 85 s with peak about 224 MiB.
9. Enabling `watch` did not pick up a newly copied book within 60 s. This is inconclusive: macOS host bind-mount events under OrbStack; Linux inotify on the VPS will likely work. I would still add autoScanCronExpression as a backstop.
10. Not tested: a real Kobo device, KOReader, OIDC with Authelia, and interaction with a live CWA instance (I did not touch the real bookstack containers).

**Cleanup:** `docker rm -f bo-audit-app bo-audit-db`, `docker volume rm bo-audit-pg`, `docker network rm bo-audit-net`, and I also removed both pulled images. The final check listed no bo-audit containers, volumes or networks.

## Integration design (optional overlay)

Target: BookOrbit as an optional **read-only second front end** over the CWA library, delivered as an overlay like Ephemera. CWA stays the only writer and the Kobo default.

**1. docker-compose.bookorbit.yml (new)**
```yaml
services:
  bookorbit:
    image: ${IMG_BOOKORBIT:-ghcr.io/bookorbit/bookorbit:3.0.0}   # seed as @sha256:571ea47b…
    container_name: bookorbit
    restart: unless-stopped
    init: true
    user: "${PUID}:${PGID}"          # entrypoint supports non-root (check_writable_current_user); pre-create dirs 1000:1000
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 512
    mem_limit: 768m
    mem_reservation: 192m
    logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }
    environment:
      - TZ=${TZ}
      - APP_URL=https://read.${DOMAIN}
      - POSTGRES_HOST=bookorbit-db
      - POSTGRES_USER=bookorbit
      - POSTGRES_DB=bookorbit
      - POSTGRES_PASSWORD=${BOOKORBIT_DB_PASSWORD}
      - JWT_SECRET=${BOOKORBIT_JWT_SECRET}
      - PODCAST_ENCRYPTION_KEY=${BOOKORBIT_PODCAST_KEY}
      - SETUP_BOOTSTRAP_TOKEN=${BOOKORBIT_SETUP_TOKEN}
      - LIBRARY_BROWSE_ROOT=/books
      - NODE_MAX_OLD_SPACE_SIZE=auto
      - TRUST_PROXY=172.16.0.0/12      # docker bridge gateway (what the app sees behind host-network Caddy); REQUIRED, see risks
    ports: ["127.0.0.1:8085:3000"]     # 3001 kuma, 8083/8084/8090 taken
    volumes:
      - ./library/books:/books/calibre:ro   # ebooks only; NOT audiobooks/podcasts/dropbox/ingest
      - ./bookorbit/data:/data
    depends_on: { bookorbit-db: { condition: service_healthy } }
  bookorbit-db:
    image: ${IMG_BOOKORBIT_DB:-pgvector/pgvector:pg18}   # pin digest
    container_name: bookorbit-db
    restart: unless-stopped
    security_opt: [no-new-privileges:true]
    mem_limit: 256m
    environment: [POSTGRES_USER=bookorbit, POSTGRES_DB=bookorbit, "POSTGRES_PASSWORD=${BOOKORBIT_DB_PASSWORD}", PGDATA=/var/lib/postgresql/data/pgdata]
    volumes: [./bookorbit/postgres:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL","pg_isready -U bookorbit -d bookorbit"], interval: 10s, retries: 10 }
    # no ports
```

**2. Caddy (caddy/Caddyfile.template)**
- Phase 1, admin-only trial: add `read.@@DOMAIN@@ { bind @@TAILSCALE_IP@@; import private_tls; import hardening; reverse_proxy 127.0.0.1:8085 }`. Websockets proxy natively. The nginx buffer notes in the docs do not apply to Caddy.
- Phase 2, public for the family: bind @@PUBLIC_IP@@ with public_tls, hardening and `# @AUTHELIA_GATE:read@`, plus a `rate_limit` zone on `POST /api/v1/auth/*`. The existing login_ratelimit only matches `/login* /api/auth/*` (Caddyfile.template:92-105), so it misses BookOrbit's /api/v1/auth/login. Add a device zone for `/api/v1/opds* /api/v1/koreader*` like books' device_auth (Caddyfile.template:117-126).
- Log redaction already covers BookOrbit: the /kobo/<32hex> regex matches its tokens (Caddyfile.template:60 vs kobo-device.service.ts:30), and Authorization and X-Auth-Key are deleted (lines 61-64).

**3. Authelia bypass**
- authelia/inject-gate.py:18-26: add `"read": "/api/v1/opds /api/v1/opds/* /api/v1/koreader /api/v1/koreader/* /api/v1/kobo/* /api/kobo/* /api/v3/* /api/UserStorage/*"`. The exact `/api/v1/opds` entry is required, and the injector's anchoring turns `/x` + `/x/*` into `^/x(/|$)`.
- authelia/configuration.yml.template: a matching `domain: read.@@DOMAIN@@ policy: bypass` rule with `^/api/v1/(opds|koreader)(/.*)?$`, `^/api/v1/kobo/.*$`, `^/api/kobo/.*$`, `^/api/v3/.*$`, `^/api/UserStorage/.*$`, placed before the two_factor rule.
- Users will still log in twice (Authelia, then BookOrbit), because there is no header auth.

**4. Accounts:** scripted local accounts from bookstack.sh. Do NOT use OIDC auto-provision.
- Users → Add also calls BookOrbit as a stored service admin: login (mind the 5/min throttle), then `POST /api/v1/users {username,name,email:"<user>@family.invalid"-style if none,permissionNames:[library_download,opds_access,kobo_sync,koreader_sync],libraryIds:[]}`.
- Pass **libraryIds [] explicitly**. Omitting it applies the default library access (user.service.ts:465-468), which exposes everything before a filter exists.
- Show the returned resetUrl to the admin.
- A Users → Repair pass, also run after each dropbox ingest or nightly, looks up the genre id for `owner:<user>`. Once it exists, it runs `PUT /api/v1/users/{id}/content-filters {includeGenreIds:[id]}` and only then `PUT /api/v1/users/{id}/libraries [1]`.
- Never grant library_upload, library_edit_metadata, library_delete_books, book_dock_access or book_request_* (writes, or Shelfmark duplication).
- Library config via API: `watch:true`, `autoScanCronExpression:"*/15 * * * *"`, `fileWriteEnabled:false`, `fileRenameEnabled:false`.

**5. Isolation:** preserved through the genre include filter, which I verified for web, OPDS and Kobo, as long as (a) every user is created by the script above and (b) sharing is done by embedding tags, not CWA-only tag edits. The admin account in BookOrbit sees everything, same as CWA.

**6. Kobo decision:** keep CWA as every family Kobo's endpoint (the existing Users-menu Kobo link, Shelfmark and portal flows all assume it). BookOrbit stays web reader + OPDS + stats. A person who really wants BookOrbit's Kobo sync (annotations, statistics) switches that one device manually and curates a Sync-to-Kobo collection. Document that per device it is either/or.

**7. Backups (scripts/backup.sh)**
- Before restic (around line 43), when the overlay is enabled: `docker exec bookorbit-db pg_dump -U bookorbit -Fc bookorbit > "$SNAP/bookorbit.pgdump"` and add it to the MANIFEST.
- Add `--exclude "$STACK_DIR/bookorbit/postgres"` (raw PGDATA copied live is inconsistent) and optionally `bookorbit/data/covers`.
- bookstack.sh step_restore (around 560-585) needs a branch: start bookorbit-db, then `pg_restore --clean --if-exists`.
- Because migrations run at start, Operations → Update must take the pre-update pg_dump, and a rollback means restoring the dump, not just retagging.

**8. TUI (bookstack.sh)**
- Add `composeB(){ … -f docker-compose.yml -f docker-compose.bookorbit.yml "$@"; }` next to composeE (line 78), and copy the overlay file in (line 121).
- Add a Library-menu item "B BookOrbit: enable/disable (read-only second reader)" in menu_library (1227-1243), modelled on step_ephemera/step_ephemera_off (1062-1105):
  - generate the 4 secrets with openssl rand -hex 32
  - `install -d -o 1000 -g 1000 bookorbit/data`, and `install -d bookorbit/postgres`
  - `cf_dns read` with TAILSCALE_IP (phase 1) or PUBLIC_IP proxied (phase 2)
  - `composeB up -d`, wait for health, run setup via the x-setup-token header, create the library via API, print the admin password
  - write BOOKORBIT_ENABLED
- Add it to stack_up_all (1110-1114), step_logs (1108), IMG_KEYS (1109: IMG_BOOKORBIT, IMG_BOOKORBIT_DB), step_deploy (around 414), the Cloudflare DNS loop and the no-cache rule hostnames (321, 342), selftest (health 200; `curl read.<domain>/api/v1/opds` → 401 Basic, not 302), and the isolation guide.
- Default image-bump policy should be manual: weekly upstream releases, and a major version yesterday.

## Conflicts and risks

Ranked by what would actually bite.

1. **[security/availability] Shared login throttle.** Without TRUST_PROXY, every request appears from the Docker gateway. Observed: `ip=192.168.117.1` on every login (main.ts:35; auth.controller.ts:83 allows 5 logins/min). Scenario: anyone on the internet posts 5 bad logins per minute to read.<domain>/api/v1/auth/login, and the whole family gets 429 indefinitely. A family member's typos lock out the others. Fix: TRUST_PROXY set to the bridge CIDR, plus a Caddy rate_limit on /api/v1/auth/*.
2. **[isolation] New and OIDC users see the whole library.** A user created before any owner:<user> book exists, or via OIDC auto-provision, has no filter and sees all family books (content-filter.repository.ts validateEntityIds; user.service.ts:465-474; oidc.service.ts around 345-355). Fix: explicit libraryIds [] and a repair step, no OIDC auto-provision.
3. **[isolation, plausible] Wrong-book file assignment.** Open scanner bug #1186/#1216 (inode reuse). CWA replaces files (EPUB fixer, conversions, deletes), so one user's file could attach to another user's book record. I did not reproduce it.
4. **[divergence] CWA-only sharing is invisible to BookOrbit.** Sharing or re-tagging done only in CWA's Calibre DB does not reach BookOrbit, because embedded subjects take precedence (libraries.ts:37-41 metadataPrecedence). The two front ends then disagree on who sees what.
5. **[duplication] Kobo endpoint.** A device can only sync with one server, so CWA's Kobo link flow (librarian/cwa.py, Users menu) and BookOrbit's are mutually exclusive.
6. **[duplication] Features the stack already has.** BookOrbit's uploads, Book Dock, book requests/indexers, Send-to-Email and KOReader sync duplicate the portal, Shelfmark, librarian Kindle mail and kosync. All must stay disabled or permission-less, or they either fail on :ro or create a second ingest path that bypasses owner tagging.
7. **[duplicates] CWA's new_record policy.** Per-user copies (README.md:165) show up in BookOrbit's duplicate detector. Merges and deletes must not be attempted; they fail on :ro.
8. **[backups] Raw PGDATA in the snapshot.** backup.sh only snapshots SQLite (lines 18-19) and backs up all of STACK_DIR (line 45), so Postgres files would be copied live and inconsistent. Restore has no pg step. Auto-migrations on start make image rollbacks one-way without a dump.
9. **[ops] One-maintainer project.** Bus factor 1, 441 open issues, weekly minors, a v3 major one day old. Expect breaking changes over a year of unattended running. A pinned digest and manual updates are mandatory.
10. **[resources] Disk and CPU, not RAM.** About 0.4 GB RAM / 1 GB fence, which the measured ≈ 1.8 GB of peak headroom on the 4 GB box absorbs; about 2 GB of the 80 GB disk, which is the binding constraint; and a scan that takes a third to a half of one of only two cores while CWA conversions want more than one.
11. **[friction] Two logins and another password.** With the Authelia gate on, users log in to Authelia then BookOrbit (no header auth). OPDS and KOReader need separate sub-account passwords on top of the CWA password: a fourth credential set for a family.
12. **[minor]** Phones home to GitHub for update checks by default (can be disabled). An additional AGPL attribution term was added on 2026-09-10 (no effect on private unmodified use). Device paths through Cloudflare have the same Bot Fight Mode caveat as the existing books./audio. hosts.

Nothing in the existing stack breaks if BookOrbit is added this way. The :ro mount means CWA, the librarian and Shelfmark are untouched. Every risk above is inside BookOrbit or in operator effort.

## Recommendation

**Trial admin-only; do not roll it out to the family yet, and do not adopt it instead of CWA.**

Why not instead of CWA:
- The whole stack is built on CWA's app.db. The librarian authenticates and writes users, Kindle addresses and Kobo tokens there (docker-compose.yml:206-210). Shelfmark uses AUTH_METHOD=cwa (docker-compose.yml:244-245). Isolation is CWA Allowed Tags (README.md:161-164).
- Replacing that with a 4-month-old, single-maintainer project with a major version released yesterday would mean rewriting the portal, Shelfmark auth, the Users menu and backups.
- BookOrbit does ship a CWA migration adapter (users, read status, Kobo/KOReader progress, shelves; server/src/modules/migration/adapters/calibre-web-automated) if that path is ever wanted.

Why not straight to the family:
- For 3-4 readers it adds a Postgres, another login (two with Authelia), OPDS/KOReader sub-passwords, and a Kobo either/or choice.
- It also needs TRUST_PROXY, filter scripting and pg backups before it is safe on the internet.
- CWA already covers library, Kobo, OPDS and Kindle, and ABS covers audio.

What a trial looks like:
- Enable it as an overlay: ebooks only, library mounted :ro, Tailscale-only at read.<domain>, pinned digest, TRUST_PROXY set, manual updates.
- The admin runs it for 3-4 weeks alongside CWA and judges whether the reader, statistics and annotation sync are worth it.

Go public (phase 2, behind the Authelia gate with the bypass list above) only if:
- the family actually wants it after seeing it,
- the scripted account and filter flow and the pg_dump backup are implemented,
- the project has stabilised (no major bump for a couple of months, #1186 fixed).

On the deployed X4, keep it off unless Ephemera is off and there is disk to spare for its ~2 GB of images plus the database and covers — the measurement says memory is not the reason to refuse it, disk and the two cores are. The recommendation itself does not change: admin-only trial first.

If it isn't worth that effort, skipping it loses nothing the family needs today.

## Sources

- https://bookorbit.app/
- https://bookorbit.app/installation
- https://bookorbit.app/library-file-structure/
- https://bookorbit.app/kobo/
- https://bookorbit.app/koreader/
- https://bookorbit.app/oidc/
- https://bookorbit.app/auth-proxies/
- https://github.com/bookorbit/bookorbit
- https://api.github.com/repos/bookorbit/bookorbit
- https://api.github.com/repos/bookorbit/bookorbit/releases
- https://api.github.com/repos/bookorbit/bookorbit/contributors
- https://api.github.com/repos/bookorbit/bookorbit/commits
- https://codeload.github.com/bookorbit/bookorbit/tar.gz/refs/tags/v3.0.0 (source read: docker-compose.yml, .env.example, Dockerfile, ADDITIONAL_TERMS.md, server/entrypoint.sh, server/src/main.ts, server/src/app.module.ts, server/src/db/schema/{libraries,auth,opds,koreader}.ts, server/src/modules/{user,auth,kobo,opds,metadata,file-write,book,book-metadata-fetch,migration}/...)
- https://github.com/bookorbit/bookorbit/issues/1186
- https://github.com/bookorbit/bookorbit/issues/1216
- https://github.com/bookorbit/bookorbit/issues/1175
- https://github.com/bookorbit/bookorbit/issues/1323
- https://github.com/bookorbit/bookorbit/issues/1397
- ghcr.io/bookorbit/bookorbit:3.0.0 @sha256:571ea47b036a8b371db9c54d54a3cea0fb96245f66c09584efac799afd339716 (pulled and run)
- pgvector/pgvector:pg18 @sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a (pulled and run)
