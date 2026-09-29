"""Credentials out of error text (v6.0.1).

A failed Usenet download's error, as Shelfmark reports it, is the whole request URL: the
seedbox SABnzbd address with its login in it (https://user:password@host), SABnzbd's API key,
and the Prowlarr link (itself URL-encoded inside the query) with Prowlarr's API key. That text
used to reach the admin's ntfy / e-mail alerts, the portal's request notes and pages as it was.
Everything the portal passes on from another service goes through secrets() first."""
import re

# user:password@ in any URL (also inside another URL's query, where it is %3A / %40 encoded)
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_USERINFO_ENC = re.compile(r"(?i)(%3A%2F%2F)[^%/\s]+(?:%3A|:)[^/\s]+?(?:%40|@)")
# key=value where the key names a secret; plain (?key=, &key=) and URL-encoded (%3Fkey%3D, %26key%3D)
_NAMES = r"(?:api[_-]?key|apikey|x-api-key|token|access[_-]?token|auth|password|passwd|pass|passkey|secret|sig|signature|nzbkey|link)"
_PLAIN = re.compile(r"(?i)((?<![a-z0-9_%-])" + _NAMES + r"=)[^&\s\"'#]+")
_ENCODED = re.compile(r"(?i)((?:%3F|%26)" + _NAMES + r"%3D)(?:(?!%26)[^&\s\"'#])+")
# 'Authorization: Bearer x', 'apikey: x' in a quoted response
_HEADER = re.compile(r"(?i)\b(authorization|x-api-key|api[_-]?key)(\s*:\s*)(?:bearer\s+|basic\s+)?[^\s,;\"'&]+")

HIDDEN = "***"


def secrets(text):
    """The same text with every credential it carries replaced by ***. Never raises."""
    if not text or not isinstance(text, str):
        return text
    try:
        t = _USERINFO.sub(lambda m: m.group(1) + HIDDEN + "@", text)
        t = _USERINFO_ENC.sub(lambda m: m.group(1) + HIDDEN + "%40", t)
        t = _ENCODED.sub(lambda m: m.group(1) + HIDDEN, t)
        t = _PLAIN.sub(lambda m: m.group(1) + HIDDEN, t)
        return _HEADER.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, t)
    except Exception:
        return "(detail hidden: it could not be checked for credentials)"


def fields(d, keys=("title", "author", "detail", "message", "status_message", "error")):
    """A copy of a dict with its text fields cleaned."""
    if not isinstance(d, dict):
        return d
    return {k: (secrets(v) if k in keys else v) for k, v in d.items()}
