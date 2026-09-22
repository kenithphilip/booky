"""All configuration comes from environment (set by bookstack.sh)."""
import os

def _bool(v, d=False):
    return str(os.environ.get(v, d)).lower() in ("1", "true", "yes", "on")

def _list(v):
    return [x.strip() for x in os.environ.get(v, "").split(",") if x.strip()]

SECRET_KEY   = os.environ.get("LIBRARIAN_SECRET", "dev-insecure-change-me")
COOKIE_SECURE = _bool("COOKIE_SECURE", True)   # always behind TLS in production; tests turn it off
CWA_DB       = os.environ.get("CWA_DB", "/cwa/app.db")          # read-only, for auth
STATE_DB     = os.environ.get("STATE_DB", "/state/librarian.db")
INGEST_DIR   = os.environ.get("INGEST_DIR", "/ingest")          # CWA watches this
STAGING_DIR  = os.environ.get("STAGING_DIR", "/staging")        # torrents land here first
AUDIO_DIR    = os.environ.get("AUDIO_DIR", "/audiobooks")       # Audiobookshelf library
OWNER_PREFIX = os.environ.get("OWNER_PREFIX", "owner:")         # tag namespace for isolation

# Request workflow (the Overseerr-style approval + notification layer)
APPROVALS_REQUIRED = _bool("APPROVALS_REQUIRED", True)  # non-admin requests wait for admin approval
NOTIFY_WEBHOOK     = os.environ.get("NOTIFY_WEBHOOK", "")  # POST JSON on new request / approval / completion
DEDUPE_WARN        = _bool("DEDUPE_WARN", True)         # warn if a title already exists in the library
ENRICH_METADATA    = _bool("ENRICH_METADATA", True)     # covers/blurbs from Open Library in the search UI
CALIBRE_DB         = os.environ.get("CALIBRE_DB", "/calibre-library/metadata.db")  # read-only dedupe check

# --- Distributed ingest / acquisition framework ---
DROPBOX_DIR   = os.environ.get("DROPBOX_DIR", "/dropbox")     # per-user subfolders, watched for files
INTAKE_TOKEN  = os.environ.get("INTAKE_TOKEN", "")            # bearer token for POST /intake automation
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "95"))    # browser upload size cap (Cloudflare Free: 100 MB bodies)
MAX_EBOOK_MB  = int(os.environ.get("MAX_EBOOK_MB", "500"))    # worker download caps, per kind ...
MAX_AUDIO_MB  = int(os.environ.get("MAX_AUDIO_MB", "2048"))   # ... (LibriVox zips of long books run past 1 GB)
GUTENBERG_MIRROR = os.environ.get("GUTENBERG_MIRROR", "")     # e.g. a local/rsynced Gutenberg mirror base URL
EBOOK_EXTS = ("epub", "mobi", "azw3", "pdf", "cbz", "cbr", "txt", "fb2")
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

# Curated catalogs offered to users. The set is fixed to sources that are free to
# redistribute — this is not a general indexer and cannot be pointed at a private tracker.
SOURCES = {
    "gutenberg":       _bool("SRC_GUTENBERG", True),
    "standard_ebooks": _bool("SRC_STANDARD",  True),
    "internet_archive":_bool("SRC_ARCHIVE",   True),
    "librivox":        _bool("SRC_LIBRIVOX",  True),
    "mycatalog":       _bool("SRC_MYCATALOG", False),   # your own self-hosted OPDS catalog
}
IA_COLLECTIONS = _list("IA_COLLECTIONS") or ["gutenberg", "opensource", "americana", "cdl"]
IA_USE_TORRENT = _bool("IA_USE_TORRENT", False)

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
ABS_TAG_ATTEMPTS = int(os.environ.get("ABS_TAG_ATTEMPTS", "24"))    # x5 s: how long to wait for ABS to scan a new item

# --- Abuse controls -----------------------------------------------------------------------
LOCKOUT_FAILS   = int(os.environ.get("LOCKOUT_FAILS", "6"))        # failed logins per user+IP ...
LOCKOUT_WINDOW  = int(os.environ.get("LOCKOUT_WINDOW", "900"))     # ... within this many seconds ...
LOCKOUT_SECONDS = int(os.environ.get("LOCKOUT_SECONDS", "900"))    # ... lock that pair for this long
LOCKOUT_IP_FAILS = int(os.environ.get("LOCKOUT_IP_FAILS", "30"))   # any usernames from one IP
MAX_REQUESTS_PER_DAY = int(os.environ.get("MAX_REQUESTS_PER_DAY", "30"))  # non-admins; 0 = unlimited
SESSION_HOURS   = int(os.environ.get("SESSION_HOURS", "12"))
TRUST_PROXY     = _bool("TRUST_PROXY", True)      # Caddy is the only thing in front (127.0.0.1 bind)
ADMIN_EMAIL     = os.environ.get("ADMIN_EMAIL", "")
KOSYNC_ENABLED  = _bool("KOSYNC_ENABLED", False)  # set by the TUI when CWA's KOReader sync is on

# --- Site URLs (for device links, the admin dashboard and Kobo sync URLs) --------------
DOMAIN      = os.environ.get("DOMAIN", "")
BOOKS_URL   = os.environ.get("BOOKS_URL")   or (f"https://books.{DOMAIN}"   if DOMAIN else "")
AUDIO_URL   = os.environ.get("AUDIO_URL")   or (f"https://audio.{DOMAIN}"   if DOMAIN else "")
SHELF_URL   = os.environ.get("SHELF_URL")   or (f"https://shelf.{DOMAIN}"   if DOMAIN else "")
ADMIN_LINKS = [  # shown on the admin dashboard; tailnet-only ones are labelled as such
    ("Calibre-Web (books)",      BOOKS_URL,                                     "public"),
    ("Audiobookshelf",           AUDIO_URL,                                     "public"),
    ("Shelfmark",                SHELF_URL,                                     "public"),
    ("qBittorrent",              f"https://dl.{DOMAIN}"       if DOMAIN else "", "tailscale"),
    ("AriaNg",                   f"https://aria.{DOMAIN}"     if DOMAIN else "", "tailscale"),
    ("Uptime Kuma",              f"https://monitor.{DOMAIN}"  if DOMAIN else "", "tailscale"),
    ("Authelia (if enabled)",    f"https://auth.{DOMAIN}"     if DOMAIN else "", "public"),
    ("Ephemera (if enabled)",    f"https://ephemera.{DOMAIN}" if DOMAIN else "", "tailscale"),
]

# --- Calibre library files (read-only) for direct download / Send-to-Kindle ----------
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", os.path.dirname(CALIBRE_DB))
FORMATS     = ("epub", "azw3", "mobi", "pdf")   # preferred-format choices (kepub only exists transiently on Kobo sync)
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

# qBittorrent Web API (P2P path). With "bypass auth for localhost" enabled in qB, no creds needed.
QBIT_URL  = os.environ.get("QBIT_URL", "http://127.0.0.1:8080")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
QBIT_CATEGORY = os.environ.get("QBIT_CATEGORY", "owned-staging")
