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
MAX_EBOOK_MB  = int(os.environ.get("MAX_EBOOK_MB", "500"))    # worker download caps, per kind ...
MAX_AUDIO_MB  = int(os.environ.get("MAX_AUDIO_MB", "2048"))   # ... (LibriVox zips of long books run past 1 GB)
# pypdf holds a whole PDF (and its clone) in memory while it embeds the owner tag; a 262 MB
# scan needed more than 384 MB. PDFs above this are parked instead of OOM-killing the portal.
MAX_PDF_MB    = int(os.environ.get("MAX_PDF_MB", "250"))
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
# The From header is trivially forged. Unless switched off, mail is filed only when the
# receiving MTA's Authentication-Results says dmarc=pass, or dkim=pass / spf=pass for the
# From domain. Turn off only for a local relay that does not add that header.
IMAP_REQUIRE_AUTH = _bool("IMAP_REQUIRE_AUTH", True)

# Curated catalogs offered to users. The set is fixed to sources that are free to
# redistribute — this is not a general indexer and cannot be pointed at a private tracker.
SOURCES = {
    "gutenberg":       _bool("SRC_GUTENBERG", True),
    "standard_ebooks": _bool("SRC_STANDARD",  False),  # its OPDS feed now answers 401 without a Patrons Circle login
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
LOCKOUT_FAILS   = int(os.environ.get("LOCKOUT_FAILS", "6"))        # failed logins per user+IP ...
LOCKOUT_WINDOW  = int(os.environ.get("LOCKOUT_WINDOW", "900"))     # ... within this many seconds ...
LOCKOUT_SECONDS = int(os.environ.get("LOCKOUT_SECONDS", "900"))    # ... lock that pair for this long
LOCKOUT_IP_FAILS = int(os.environ.get("LOCKOUT_IP_FAILS", "30"))   # any usernames from one IP
MAX_REQUESTS_PER_DAY = int(os.environ.get("MAX_REQUESTS_PER_DAY", "30"))  # non-admins; 0 = unlimited
SESSION_HOURS   = int(os.environ.get("SESSION_HOURS", "12"))
TRUST_PROXY     = _bool("TRUST_PROXY", True)      # Caddy is the only thing in front (127.0.0.1 bind)
ADMIN_EMAIL     = os.environ.get("ADMIN_EMAIL", "")
KOSYNC_ENABLED  = _bool("KOSYNC_ENABLED", False)  # set by the TUI when CWA's KOReader sync is on
AUTHELIA_ENABLED = _bool("AUTHELIA_ENABLED", False)  # users are then managed in the TUI only (Authelia has its own user file)
TORRENTS_ENABLED = _bool("TORRENTS_ENABLED", False)  # qBittorrent is an opt-in compose profile

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
        ("Ephemera (if enabled)",    f"https://ephemera.{d}" if d else "",   "tailscale"),
    ]
    return [link for link in links if link[1]]

# --- Calibre library files (read-only) for direct download / Send-to-Kindle ----------
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", os.path.dirname(CALIBRE_DB))
FORMATS     = ("epub", "azw3", "mobi", "pdf")   # preferred-format choices (kepub is made by CWA on Kobo sync, not chosen)
DOWNLOAD_FORMATS = FORMATS + ("kepub", "txt", "cbz", "cbr", "fb2", "djvu")   # what /download serves if present
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
