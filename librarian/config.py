"""All configuration comes from environment (set by bookstack.sh)."""
import os

def _bool(v, d=False):
    return str(os.environ.get(v, d)).lower() in ("1", "true", "yes", "on")

def _list(v):
    return [x.strip() for x in os.environ.get(v, "").split(",") if x.strip()]

SECRET_KEY   = os.environ.get("LIBRARIAN_SECRET", "dev-insecure-change-me")
COOKIE_SECURE = _bool("COOKIE_SECURE", True)   # always behind TLS in production; tests turn it off
# Baked into the image by the Dockerfile (ARG BUILD_VERSION -> ENV); shown on /admin and in
# /healthz?detail=1 so a stale portal image cannot be mistaken for the checkout.
BUILD_VERSION = os.environ.get("BUILD_VERSION", "") or "unknown"
CWA_DB       = os.environ.get("CWA_DB", "/cwa/app.db")          # read-only, for auth
STATE_DB     = os.environ.get("STATE_DB", "/state/librarian.db")
INGEST_DIR   = os.environ.get("INGEST_DIR", "/ingest")          # CWA watches this
STAGING_DIR  = os.environ.get("STAGING_DIR", "/staging")        # worker temp downloads + quarantine
AUDIO_DIR    = os.environ.get("AUDIO_DIR", "/audiobooks")       # Audiobookshelf library
OWNER_PREFIX = os.environ.get("OWNER_PREFIX", "owner:")         # tag namespace for isolation

# Request workflow (the Overseerr-style approval + notification layer)
# Family default: off. It only ever gated the public-domain catalog search (not Shelfmark,
# uploads, dropbox or /intake), so it added friction without protecting anything.
APPROVALS_REQUIRED = _bool("APPROVALS_REQUIRED", False)  # non-admin catalog requests wait for admin approval
NOTIFY_WEBHOOK     = os.environ.get("NOTIFY_WEBHOOK", "")  # POST on new request / approval / completion / alert
# "json" (generic webhook), "ntfy" (plain-text body + Title/Priority headers) or "auto" (ntfy
# when the host name contains "ntfy", which is what the installer's Alerts step suggests).
NOTIFY_WEBHOOK_FORMAT = os.environ.get("NOTIFY_WEBHOOK_FORMAT", "auto").lower()
DEDUPE_WARN        = _bool("DEDUPE_WARN", True)         # warn if a title already exists in the library
# Family sharing (share.py): a book already in the library is given to the next reader who asks
# (their owner tag added to the same copy) instead of being downloaded again.
FAMILY_SHARING     = _bool("FAMILY_SHARING", True)
# A book no reader has any more is deleted from the VPS after this many days (its seedbox copy,
# if any, stays on the seedbox, and a new request brings it back). 0 = never.
LIBRARY_RELEASE_DAYS = int(os.environ.get("LIBRARY_RELEASE_DAYS", "7") or 7)
ENRICH_METADATA    = _bool("ENRICH_METADATA", True)     # covers/blurbs from Open Library in the search UI
CALIBRE_DB         = os.environ.get("CALIBRE_DB", "/calibre-library/metadata.db")  # read-only dedupe check
# Calibre-Web itself (loopback; the portal shares the host network). Used for a cached
# liveness probe reported on /admin and in /healthz detail — empty switches the probe off.
CWA_URL            = os.environ.get("CWA_URL", "http://127.0.0.1:8083")
# CWA's ingest bookkeeping beside app.db: processed_books/failed holds the files its importer
# refused, which is how the portal turns a silent CWA import failure into a visible error.
CWA_PROCESSED_DIR  = os.environ.get("CWA_PROCESSED_DIR", "") or os.path.join(os.path.dirname(CWA_DB) or ".", "processed_books")

# --- Distributed ingest / acquisition framework ---
DROPBOX_DIR   = os.environ.get("DROPBOX_DIR", "/dropbox")     # per-user subfolders, watched for files
INTAKE_TOKEN  = os.environ.get("INTAKE_TOKEN", "")            # bearer token for POST /intake automation
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "95"))    # browser upload size cap (Cloudflare Free: 100 MB bodies)
MAX_UPLOAD_TAILNET_MB = int(os.environ.get("MAX_UPLOAD_TAILNET_MB", "2048") or 2048)   # L18: upload.<domain>, Tailscale only
MAX_EBOOK_MB  = int(os.environ.get("MAX_EBOOK_MB", "200"))    # worker download caps, per kind ...
MAX_AUDIO_MB  = int(os.environ.get("MAX_AUDIO_MB", "2048"))   # ... (LibriVox zips of long books run past 1 GB)
# pypdf holds a whole PDF (and its clone) in memory while it embeds the owner tag; a 262 MB
# scan needed more than 384 MB. PDFs above this are parked instead of OOM-killing the portal.
MAX_PDF_MB    = int(os.environ.get("MAX_PDF_MB", "250"))
# Mail is NOT an upload: email.message_from_bytes + get_payload(decode=True) costs roughly a
# dozen times the attachment in RSS, and the portal runs under mem_limit 1g. A 94 MB
# attachment that passed MAX_UPLOAD_MB SIGKILLed the container (and BODY.PEEK left the message
# unseen, so it came back every 60 s). Keep this well under MAX_UPLOAD_MB.
MAX_MAIL_MB   = int(os.environ.get("MAX_MAIL_MB", "40"))
GUTENBERG_MIRROR = os.environ.get("GUTENBERG_MIRROR", "")     # e.g. a local/rsynced Gutenberg mirror base URL
EBOOK_EXTS = ("epub", "mobi", "azw3", "pdf", "cbz", "cbr", "cb7", "txt", "fb2")
AUDIO_EXTS = ("mp3", "m4b", "m4a", "flac", "ogg", "opus", "aac", "wav", "zip")

# --- Email-to-library (optional IMAP intake) ---
IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "0") or 0)        # 0 = default for the mode (993 / 143)
IMAP_SSL  = _bool("IMAP_SSL", True)                            # False only for a local/private relay
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASS = os.environ.get("IMAP_PASS", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
IMAP_DEFAULT_USER = os.environ.get("IMAP_DEFAULT_USER", "")   # owner if no plus-address match
IMAP_ALLOWED_SENDERS = [s.lower() for s in _list("IMAP_ALLOWED_SENDERS")]  # else From must be the user's own e-mail
# The From header is trivially forged. Unless switched off, mail is filed only when the
# receiving MTA's Authentication-Results says dmarc=pass, or dkim=pass / spf=pass for the
# From domain. Turn off only for a local relay that does not add that header.
IMAP_REQUIRE_AUTH = _bool("IMAP_REQUIRE_AUTH", True)

# Curated catalogs offered to users. The set is fixed to sources that are free to
# redistribute — this is not a general indexer and cannot be pointed at a private tracker.
# Descriptive-metadata providers, in chain order. Separate from SOURCES above: those are
# CONTENT sources (where a book comes from); these only describe one. Keyless first, because a
# key the admin must create, store and rotate is a provider that fails on a date nobody wrote
# down. The chain runs in the BACKGROUND only — the first provider's cold path measured 28.4 s,
# which is more than twice the search page's whole deadline.
METADATA_PROVIDERS = {
    "bookinfo":    _bool("META_BOOKINFO",   True),   # rreading-glasses; ISBN identity + series
    "hardcover":   _bool("META_HARDCOVER",  True),   # same software, community-curated data
    "openlibrary": _bool("META_OPENLIBRARY", True),  # keyless floor, no token to expire
}
METADATA_ENABLED = _bool("METADATA_ENABLED", True)

SOURCES = {
    "gutenberg":       _bool("SRC_GUTENBERG", True),
    # its OPDS search feed needs a Patrons Circle login, but its downloads are public: book pages
    # reach them through Open Library's cross-links (bookmeta.py), so it is on by default again
    "standard_ebooks": _bool("SRC_STANDARD",  True),
    "internet_archive":_bool("SRC_ARCHIVE",   True),
    "librivox":        _bool("SRC_LIBRIVOX",  True),
    "mycatalog":       _bool("SRC_MYCATALOG", False),   # your own self-hosted OPDS catalog
}
IA_COLLECTIONS = _list("IA_COLLECTIONS") or ["gutenberg", "opensource", "americana", "cdl"]

# What a family member should read instead of an internal source id (search results, the
# request list, the admin queue). Anything not listed falls back to the id with '_' -> ' '.
SOURCE_LABELS = {
    "gutenberg": "Project Gutenberg", "standard_ebooks": "Standard Ebooks",
    "internet_archive": "Internet Archive", "librivox": "LibriVox",
    "dropbox": "Your upload", "intake": "Automation", "imap": "E-mailed in",
}

def source_label(source):
    if source == "mycatalog":
        return MYCATALOG_NAME
    if (source or "").startswith("opds:"):
        try:
            import catalogs
            c = catalogs.get(source)
            return c["name"] if c else source[5:]
        except Exception:
            return source[5:]
    return SOURCE_LABELS.get(source) or (source or "?").replace("_", " ")

# --- Your own catalog (OPDS) -------------------------------------------------
# For your own writing: point this at any OPDS feed you host (Calibre's content server,
# Calibre-Web, Kavita, Komga, BookLore, Ubooquity...). One username/password (HTTP Basic)
# matches a single-user server. OPDS is the standard book-catalog protocol — not a tracker.
MYCATALOG_NAME = os.environ.get("MYCATALOG_NAME", "My catalog")
MYCATALOG_URL  = os.environ.get("MYCATALOG_URL", "")   # OPDS feed; may contain {q} for search
MYCATALOG_USER = os.environ.get("MYCATALOG_USER", "")
MYCATALOG_PASS = os.environ.get("MYCATALOG_PASS", "")

# --- Audiobookshelf API (optional) -------------------------------------------
# If set, the worker triggers a library scan after placing an audiobook. Per-user audiobook
# isolation uses ABS's own tag restriction (see README); tagging can be done in the ABS UI.
ABS_URL   = os.environ.get("ABS_URL", "http://127.0.0.1:13378")
ABS_TOKEN = os.environ.get("ABS_TOKEN", "")                       # an ABS API key (no expiry), from the TUI's ABS setup
ABS_LIBRARY_NAME = os.environ.get("ABS_LIBRARY_NAME", "Audiobooks")
ABS_LIBRARY_PATH = os.environ.get("ABS_LIBRARY_PATH", "/audiobooks")  # path INSIDE the ABS container
ABS_TAG_ATTEMPTS = int(os.environ.get("ABS_TAG_ATTEMPTS", "24"))    # x5 s: `python -m abs tag` wait for ABS to scan a new item
ABS_TAG_GIVE_UP_HOURS = int(os.environ.get("ABS_TAG_GIVE_UP_HOURS", "24"))   # worker: persistent tag jobs retried this long

# --- Abuse controls -----------------------------------------------------------------------
LOCKOUT_FAILS   = int(os.environ.get("LOCKOUT_FAILS", "5"))        # failed logins per user+IP ...
LOCKOUT_WINDOW  = int(os.environ.get("LOCKOUT_WINDOW", "900"))     # ... within this many seconds ...
LOCKOUT_SECONDS = int(os.environ.get("LOCKOUT_SECONDS", "900"))    # ... lock that pair for this long
LOCKOUT_IP_FAILS = int(os.environ.get("LOCKOUT_IP_FAILS", "20"))   # any usernames from one IP
MAX_REQUESTS_PER_DAY = int(os.environ.get("MAX_REQUESTS_PER_DAY", "30"))  # non-admins; 0 = unlimited
# Keep looking (wanted.py): a reader may leave this many open "keep looking" entries; each is
# given up (and the reader told) after WANTED_DAYS without a match.
WANTED_MAX_PER_USER = int(os.environ.get("WANTED_MAX_PER_USER", "25") or 25)
WANTED_DAYS = int(os.environ.get("WANTED_DAYS", "180") or 180)
# The language a reader reads in, by default (each reader can change theirs on Devices). A copy
# in another language is never requested automatically. ISO 639-1 codes.
LANGUAGES = {"en": "English", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
             "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "fi": "Finnish", "pl": "Polish",
             "ru": "Russian", "hi": "Hindi", "ml": "Malayalam", "ta": "Tamil", "zh": "Chinese",
             "ja": "Japanese", "la": "Latin", "el": "Greek"}
BOOK_LANGUAGE = (os.environ.get("BOOK_LANGUAGE") or os.environ.get("SHELFMARK_LANGUAGE") or "en").lower()
if BOOK_LANGUAGE not in LANGUAGES:
    BOOK_LANGUAGE = "en"
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
# L16: the portal reads and decides Shelfmark's pending requests (shelfmark_api.py)
# L17: optional Cloudflare Turnstile on the portal login (Security -> Login bot check). Off
# unless both are set; when on, the login page alone may load Cloudflare's challenge script.
# L08: the synthetic journey's two accounts (Operations -> Canary journey). Hidden from every user
# list; the host job alone logs in as them.
CANARY_USERS = tuple(n.strip() for n in os.environ.get("CANARY_USERS", "").split(",") if n.strip())
TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
SHELFMARK_API = os.environ.get("SHELFMARK_API", "http://127.0.0.1:8084").rstrip("/")
SHELFMARK_SVC_USER = os.environ.get("SHELFMARK_SVC_USER", "")
SHELFMARK_SVC_PASS = os.environ.get("SHELFMARK_SVC_PASS", "")
# L05: "proxy" while the Authelia gate is on (Shelfmark then trusts Remote-User from Caddy); the
# portal, a host process Shelfmark trusts the same way, then sends the service name as the header
SHELFMARK_AUTH_METHOD = (os.environ.get("SHELFMARK_AUTH_METHOD") or "cwa").strip().lower()
ABS_OIDC_SECRET = os.environ.get("ABS_OIDC_SECRET", "")          # L05: Audiobookshelf's client secret at Authelia
HARDCOVER_API_KEY = os.environ.get("HARDCOVER_API_KEY", "")
SESSION_HOURS   = int(os.environ.get("SESSION_HOURS", "12"))
TRUST_PROXY     = _bool("TRUST_PROXY", True)      # Caddy is the only thing in front (127.0.0.1 bind)
ADMIN_EMAIL     = os.environ.get("ADMIN_EMAIL", "")
KOSYNC_ENABLED  = _bool("KOSYNC_ENABLED", False)  # set by the TUI when CWA's KOReader sync is on
AUTHELIA_ENABLED = _bool("AUTHELIA_ENABLED", False)
# L05: with the gate on, Caddy adds X-Bookstack-Gate: <GATE_SECRET> to requests Authelia let
# through (and strips any the client sent). Only then is Remote-User trusted: one login.
GATE_SECRET = os.environ.get("GATE_SECRET", "")  # users are then managed in the TUI only (Authelia has its own user file)
TORRENTS_ENABLED = _bool("TORRENTS_ENABLED", False)  # qBittorrent is an opt-in compose profile
EPHEMERA_ENABLED = _bool("EPHEMERA_ENABLED", False)  # ditto; its vhost only exists when it is on

# --- Site URLs (for device links, the admin dashboard and Kobo sync URLs) --------------
DOMAIN      = os.environ.get("DOMAIN", "")
BOOKS_URL   = os.environ.get("BOOKS_URL")   or (f"https://books.{DOMAIN}"   if DOMAIN else "")
AUDIO_URL   = os.environ.get("AUDIO_URL")   or (f"https://audio.{DOMAIN}"   if DOMAIN else "")
SHELF_URL   = os.environ.get("SHELF_URL")   or (f"https://shelf.{DOMAIN}"   if DOMAIN else "")
def admin_links():
    """Shown on the admin dashboard; tailnet-only ones are labelled as such."""
    d = DOMAIN
    links = [
        ("Calibre-Web (books)",      BOOKS_URL,                              "public"),
        ("Audiobookshelf",           AUDIO_URL,                              "public"),
        ("Shelfmark",                SHELF_URL,                              "public"),
        ("qBittorrent",              f"https://dl.{d}" if d and TORRENTS_ENABLED else "", "tailscale"),
        ("Uptime Kuma",              f"https://monitor.{d}"  if d else "",   "tailscale"),
        ("Authelia",                 f"https://auth.{d}" if d and AUTHELIA_ENABLED else "", "public"),
        # only when it is switched on: render_caddyfile drops the vhost and step_cloudflare
        # creates no DNS record otherwise, so the link resolved to nothing on a default install
        ("Ephemera",                 f"https://ephemera.{d}" if d and EPHEMERA_ENABLED else "", "tailscale"),
    ]
    return [link for link in links if link[1]]

# --- Calibre library files (read-only) for direct download / Send-to-Kindle ----------
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", os.path.dirname(CALIBRE_DB))
import shutil as _shutil
KEPUBIFY = _shutil.which("kepubify") or ""
# preferred-format choices. 'kepub' is offered when the portal image carries kepubify (L11): the
# portal converts on download, one book at a time, into a size-capped cache.
FORMATS     = ("epub", "azw3", "mobi", "pdf") + (("kepub",) if KEPUBIFY else ())
KEPUB_CACHE_DIR = os.environ.get("KEPUB_CACHE_DIR", os.path.join(os.path.dirname(os.environ.get("STATE_DB", "/state/librarian.db")), "kepub"))
KEPUB_CACHE_MB = int(os.environ.get("KEPUB_CACHE_MB", "512") or 512)
# On-demand conversion ("Convert to…" on a book's page): Calibre's own ebook-convert, run by
# the host job inside the CWA container, one at a time. Heavy on 2 cores, hence a daily limit.
CONVERT_TARGETS = ("epub", "azw3", "mobi", "pdf", "txt", "docx", "fb2", "rtf")
CONVERT_SOURCES = ("epub", "azw3", "mobi", "fb2", "docx", "rtf", "txt", "pdf")   # best source first
CONVERT_MAX_PER_DAY = int(os.environ.get("CONVERT_MAX_PER_DAY", "10") or 10)
# What /download serves if the file is there. 'kepub' stays in this list on purpose even
# though nothing in this stack produces one today: CWA v4.0.6 only autodetects kepubify at
# /opt/kepubify/kepubify-linux-{64,32}bit and its image installs it at /usr/bin/kepubify, so
# config_kepubifypath is permanently empty and Kobo sync ships plain EPUB. If an admin ever
# pre-generates KEPUBs (Convert Library, with nothing syncing — see docs/DECISIONS-PENDING.md),
# the serving half here and in library.py already works.
DOWNLOAD_FORMATS = FORMATS + ("kepub", "txt", "cbz", "cbr", "fb2", "djvu")
DEFAULT_FORMAT = "epub"
KINDLE_FORMATS = ("epub", "pdf", "txt")         # what Amazon's Send-to-Kindle mail accepts (MOBI/AZW3 are bounced)

# --- Outgoing mail (Send-to-Kindle from the portal) ----------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()   # starttls | ssl | none
KINDLE_MAX_MB = int(os.environ.get("KINDLE_MAX_MB", "45"))
KINDLE_DEFAULT_LANG = os.environ.get("KINDLE_DEFAULT_LANG", "en")   # dc:language added when an EPUB has none (Amazon bounces those)
# Ceiling on Send-to-Kindle per non-admin per 24 h, in the spirit of MAX_REQUESTS_PER_DAY.
# Nothing else limited outbound mail: neither route is matched by a Caddy rate_limit zone, and
# kindle.send only checks the attachment size, so one session cookie could drive unlimited
# 45 MB messages through the configured SMTP account. The realistic outcome is not a breach but
# the provider suspending the account or Amazon dropping the approved sender — which kills
# Send-to-Kindle, mail notifications AND scripts/alert.sh's fallback channel at the same time.
# 20 is far above a reader's real use (a family of four sends a handful a week). 0 = unlimited.
KINDLE_MAX_PER_DAY = int(os.environ.get("KINDLE_MAX_PER_DAY", "20"))
KINDLE_TEST_COOLDOWN = 300   # seconds between "send a test to my Kindle" clicks, per user

# --- Comics and manga (docs/COMICS.md) -------------------------------------------------
# The Comics page, searched through Shelfmark and delivered to the readers' devices. Western
# comics are described by Metron (free account) with ComicVine as a fallback (free key), manga /
# manhwa / manhua by MangaUpdates (no key). A comic arrives as a CBZ in the reader's dropbox,
# like any Shelfmark download; the host job scripts/comic-convert.sh adds the Kobo copy (KCC).
COMICS_ENABLED = _bool("COMICS_ENABLED", False)
METRON_USER = os.environ.get("METRON_USER", "")
METRON_PASS = os.environ.get("METRON_PASS", "")
COMICVINE_API_KEY = os.environ.get("COMICVINE_API_KEY", "")
COMIC_EXTS = ("cbz", "cbr", "cb7")
# A comic request nothing matched is searched again after 1 h, 6 h, then daily, for this long.
COMIC_SEARCH_DAYS = int(os.environ.get("COMIC_SEARCH_DAYS", "30") or 30)
COMIC_MAX_OPEN_PER_USER = int(os.environ.get("COMIC_MAX_OPEN_PER_USER", "50") or 50)
# A download Shelfmark accepted but whose file never arrived: searched again (another release)
COMIC_ARRIVAL_HOURS = int(os.environ.get("COMIC_ARRIVAL_HOURS", "24") or 24)
# CBR/CB7 are repacked as CBZ with The Unarchiver's unar (every RAR version, solid ones included,
# and 7z); libarchive's bsdtar is the fallback (it cannot open solid RAR 3/4 archives)
UNAR = os.environ.get("UNAR", "unar")
LSAR = os.environ.get("LSAR", "lsar")
BSDTAR = os.environ.get("BSDTAR", "bsdtar")
