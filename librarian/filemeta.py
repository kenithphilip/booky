"""Title and author out of the formats that cannot carry the owner tag (MOBI/AZW3, FB2), so
the host job can find the book in Calibre after the import (worker._untagged_match), even when
CWA converted it to EPUB on the way in and Calibre named it after its own metadata.

Read-only and bounded: at most the first HEAD bytes of a file are read, and nothing is parsed
as XML (an FB2's <title-info> is picked out with regular expressions, so no entity expansion
can happen). Anything unexpected returns {} and the caller falls back to the file name.
"""
import re
import struct

HEAD = 4 * 2**20            # MOBI record 0 and FB2 <description> are near the start


def read(path, ext):
    """{'title': ..., 'author': ...} (either may be missing), or {}."""
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD)
    except OSError:
        return {}
    try:
        if ext in ("mobi", "azw3", "azw", "prc"):
            return _mobi(data)
        if ext == "fb2":
            return _fb2(data)
    except (ValueError, struct.error, IndexError, LookupError, UnicodeError):
        pass
    return {}


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _mobi(b):
    """PalmDB -> record 0 -> MOBI header -> EXTH 503 (title) / 100 (author), else the MOBI
    header's full name."""
    if len(b) < 78 or b[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        return {}
    n = struct.unpack(">H", b[76:78])[0]
    if n < 1:
        return {}
    r0 = struct.unpack(">I", b[78:82])[0]
    mobi = r0 + 16
    if b[mobi:mobi + 4] != b"MOBI":
        return {}
    hlen = struct.unpack(">I", b[mobi + 4:mobi + 8])[0]
    enc = {65001: "utf-8", 1252: "cp1252"}.get(struct.unpack(">I", b[mobi + 12:mobi + 16])[0], "utf-8")
    name_off, name_len = struct.unpack(">II", b[r0 + 84:r0 + 92])
    out = {}
    full = b[r0 + name_off:r0 + name_off + name_len] if name_len < 4096 else b""
    if full:
        out["title"] = _clean(full.decode(enc, "replace"))
    flags = struct.unpack(">I", b[r0 + 128:r0 + 132])[0] if len(b) >= r0 + 132 else 0
    ex = mobi + hlen
    if flags & 0x40 and b[ex:ex + 4] == b"EXTH":
        count = struct.unpack(">I", b[ex + 8:ex + 12])[0]
        p, authors = ex + 12, []
        for _ in range(min(count, 1000)):
            kind, size = struct.unpack(">II", b[p:p + 8])
            if size < 8:
                break
            val = _clean(b[p + 8:p + size].decode(enc, "replace"))
            if kind == 503 and val:
                out["title"] = val
            elif kind == 100 and val:
                authors.append(val)
            p += size
        if authors:
            out["author"] = " & ".join(authors)
    return {k: v for k, v in out.items() if v}


_ENC = re.compile(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9_.-]+)["\']')


def _fb2(b):
    m = _ENC.search(b[:200])
    text = b.decode(m.group(1).decode() if m else "utf-8", "replace")
    ti = re.search(r"<(?:\w+:)?title-info\b.*?</(?:\w+:)?title-info>", text, re.S)
    if not ti:
        return {}
    block = ti.group(0)

    def tag(name, src):
        t = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", src, re.S)
        return _clean(re.sub(r"<[^>]+>", " ", t.group(1))) if t else ""

    out = {"title": tag("book-title", block)}
    names = []
    for a in re.findall(r"<(?:\w+:)?author\b[^>]*>(.*?)</(?:\w+:)?author>", block, re.S)[:5]:
        n = " ".join(x for x in (tag("first-name", a), tag("middle-name", a), tag("last-name", a)) if x) or tag("nickname", a)
        if n:
            names.append(n)
    if names:
        out["author"] = " & ".join(names)
    return {k: v for k, v in out.items() if v}
