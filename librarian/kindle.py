"""Send-to-Kindle from the portal: e-mails a book file to the user's Kindle address (the one
stored in Calibre-Web, so it is the same address CWA uses). Amazon must have the SMTP_FROM
sender on the user's approved list. By e-mail Amazon accepts EPUB, PDF (and TXT/DOC/HTML);
MOBI/AZW were dropped in 2022 and AZW3 was never accepted, so those are refused here."""
import os, smtplib, ssl, mimetypes, zipfile, tempfile, logging
from email.message import EmailMessage
from lxml import etree
import config, tagger

log = logging.getLogger("kindle")

def kindle_ready(path, title=None, author=None):
    """Return (file_to_send, note). For an EPUB, apply on a temporary copy the two repairs
    Amazon's Send-to-Kindle most often bounces a file for: no dc:language in the OPF (defaults
    to KINDLE_DEFAULT_LANG) and XHTML files without an XML encoding declaration. Everything
    else is copied byte-for-byte with its own compression (mimetype first and stored). This is
    what CWA's "Kindle EPUB fixer" does on import; it is applied here instead because on import
    that fixer rewrites every archive and strips the owner tag from comics. On any problem the
    original file is sent unchanged: a fix must never block delivery.

    `title`/`author` are what the LIBRARY says (Calibre's current record — fixed by the metadata
    push, or corrected by hand). Amazon reads the title from the file, and calibredb updates the
    database but never the OPF inside the EPUB, so without this a book whose file still carries
    its release name arrives on the Kindle as 'Melville.Moby.Dick.RETAIL'. Applied to the temp
    copy only; the stored file is never touched."""
    if not path.lower().endswith(".epub"):
        return path, ""
    tmp = None
    try:
        with zipfile.ZipFile(path) as zin:
            opf = tagger._opf_path(zin)
            root = etree.fromstring(zin.read(opf))
            changes, kinds = {}, set()
            meta = root.find(f"{{{tagger.OPF_NS}}}metadata")
            if meta is None:
                meta = etree.SubElement(root, f"{{{tagger.OPF_NS}}}metadata")
            if not any((el.text or "").strip() for el in meta.findall(f"{{{tagger.DC_NS}}}language")):
                lang = etree.SubElement(meta, f"{{{tagger.DC_NS}}}language")
                lang.text = config.KINDLE_DEFAULT_LANG
                kinds.add("language")
            for tag, want in (("title", title), ("creator", author)):
                want = (want or "").strip()
                if not want:
                    continue
                els = meta.findall(f"{{{tagger.DC_NS}}}{tag}")
                if els and (els[0].text or "").strip() == want:
                    continue
                el = els[0] if els else etree.SubElement(meta, f"{{{tagger.DC_NS}}}{tag}")
                el.text = want
                kinds.add(tag)
            if kinds & {"language", "title", "creator"}:
                changes[opf] = etree.tostring(root, xml_declaration=True, encoding="utf-8", standalone=False)
            for info in zin.infolist():
                if info.filename == opf or not info.filename.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                data = zin.read(info)
                if data.lstrip().startswith(b"<?xml") or data.startswith(b"\xef\xbb\xbf"):
                    continue
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    continue                                   # not UTF-8: a declaration would lie
                changes[info.filename] = b'<?xml version="1.0" encoding="utf-8"?>\n' + data
                kinds.add("encoding")
            if not changes:
                return path, ""
            fd, tmp = tempfile.mkstemp(suffix=".epub")
            os.close(fd)
            infos = sorted(zin.infolist(), key=lambda i: 0 if i.filename == "mimetype" else 1)
            with zipfile.ZipFile(tmp, "w") as zout:
                for info in infos:
                    if info.filename in changes:
                        zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zi.external_attr = info.external_attr
                        zout.writestr(zi, changes[info.filename])
                    else:
                        zout.writestr(info, zin.read(info))
        return tmp, "; Kindle fixes applied: " + ", ".join(sorted(kinds))
    except Exception as e:                                     # noqa: BLE001 - never block delivery
        log.warning("kindle fixes skipped for %s: %s", os.path.basename(path), e)
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        return path, ""

class MailNotConfigured(Exception):
    pass

def configured():
    return bool(config.SMTP_HOST and config.SMTP_FROM)

def send(to_addr, path, title=None, filename=None, author=None, book_title=None):
    """`title` is the MAIL SUBJECT. `book_title`/`author` are what the LIBRARY says the book is,
    and only they rewrite the Kindle copy's metadata. Kept apart on purpose: the auto-Kindle path
    passes a subject that can be a bare filename before import, and stamping that into the book's
    dc:title would make the Kindle copy worse than the file it came from."""
    if not configured():
        raise MailNotConfigured("outgoing mail is not configured (SMTP_HOST/SMTP_FROM)")
    if not to_addr or "@" not in to_addr:
        raise ValueError("no Kindle address on file")
    filename = filename or os.path.basename(path)
    if not filename.lower().endswith(tuple("." + f for f in config.KINDLE_FORMATS)):
        raise ValueError("Amazon accepts EPUB or PDF by e-mail (not MOBI/AZW3)")
    size = os.path.getsize(path)
    if size > config.KINDLE_MAX_MB * 1024 * 1024:
        raise ValueError(f"file is {size // 1048576} MB; Amazon rejects attachments over {config.KINDLE_MAX_MB} MB")
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if filename.lower().endswith(".epub"):
        ctype = "application/epub+zip"
    maintype, subtype = ctype.split("/", 1)
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = title or filename
    msg.set_content("Sent from your library.")
    send_path, note = kindle_ready(path, title=book_title, author=author)
    try:
        with open(send_path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=filename)
    finally:
        if send_path != path:
            os.unlink(send_path)
    _deliver(msg)
    return f"sent to {to_addr}{note}"

def send_test(to_addr):
    """`python -m kindle test addr@example.com` — used by the installer's Mail menu."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("If you can read this, Send-to-Kindle mail from your library works.\n")
        p = f.name
    try:
        return send(to_addr, p, "Library mail test", "library-test.txt")
    finally:
        os.unlink(p)

def _deliver(msg):
    ctx = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=ctx) as s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
        return
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        if config.SMTP_SECURITY == "starttls":
            s.starttls(context=ctx)
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASS)
        s.send_message(msg)

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "test":
        try:
            print(send_test(sys.argv[2])); sys.exit(0)
        except Exception as e:
            print(f"FAILED: {e}"); sys.exit(1)
    print("usage: python -m kindle test <address>"); sys.exit(2)
