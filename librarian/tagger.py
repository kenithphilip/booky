"""Write an owner tag into a book's metadata *before* import, so Calibre-Web's per-user tag
restriction makes the book visible (and syncable) only to that owner. No post-import
database writes are needed. Formats: EPUB (dc:subject in the OPF), PDF (Info /Keywords,
which Calibre maps to tags) and CBZ (a ComicBookInfo/1.0 JSON block in the zip comment,
which Calibre's comic reader maps to tags). Verified against Calibre 9.1 (ebook-meta)."""
import zipfile, os, re, json, datetime
from urllib.parse import unquote
from lxml import etree
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS  = "http://purl.org/dc/elements/1.1/"
CONT_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

class TagError(Exception):
    """The file's structure does not let us embed the tag (not a valid EPUB/PDF/CBZ)."""

def _opf_path(z):
    try:
        root = etree.fromstring(z.read("META-INF/container.xml"))
    except (KeyError, etree.XMLSyntaxError) as e:
        raise TagError(f"no usable META-INF/container.xml ({e.__class__.__name__})")
    rf = root.find(f".//{{{CONT_NS}}}rootfile")
    path = unquote(rf.get("full-path") or "") if rf is not None else ""
    names = z.namelist()
    if path in names:
        return path
    hit = next((n for n in names if n.lower() == path.lower()), None)   # sloppy producers
    if not hit:
        raise TagError("OPF not found")
    return hit

def add_owner_tag(epub_path, owner_tag):
    tmp = epub_path + ".tmp"
    try:
        zin = zipfile.ZipFile(epub_path, "r")
    except zipfile.BadZipFile as e:
        raise TagError(f"not a zip: {e}")
    with zin:
        opf = _opf_path(zin)
        try:
            root = etree.fromstring(zin.read(opf))
        except etree.XMLSyntaxError as e:
            raise TagError(f"OPF is not well-formed XML: {e}")
        meta = root.find(f"{{{OPF_NS}}}metadata")
        if meta is None:
            meta = etree.SubElement(root, f"{{{OPF_NS}}}metadata")
        exists = any((s.text or "").strip() == owner_tag for s in meta.findall(f"{{{DC_NS}}}subject"))
        if not exists:
            subj = etree.SubElement(meta, f"{{{DC_NS}}}subject")
            subj.text = owner_tag
        new_opf = etree.tostring(root, xml_declaration=True, encoding="utf-8", standalone=False)
        # mimetype must stay first (and stored); every other entry keeps its own compression,
        # timestamp and attributes so the file does not balloon (a re-zip with ZIP_STORED
        # inflated some EPUBs 3-300x and pushed them over the Kindle mail limit).
        infos = sorted(zin.infolist(), key=lambda i: 0 if i.filename == "mimetype" else 1)
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                for info in infos:
                    if info.filename == opf:
                        zi = zipfile.ZipInfo(opf, date_time=info.date_time)
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zi.external_attr = info.external_attr
                        zout.writestr(zi, new_opf)
                    else:
                        zout.writestr(info, zin.read(info))
        except (zipfile.BadZipFile, OSError) as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise TagError(f"cannot rewrite EPUB ({e})")
    os.replace(tmp, epub_path)

def _split_keywords(s):
    return [k.strip() for k in re.split(r"[,;]", s or "") if k.strip()]

def add_owner_tag_pdf(pdf_path, owner_tag, title=None):
    """Merge the tag into the Info dictionary's /Keywords (comma-separated, existing kept).
    `title` fills in /Title only when the PDF has none (Calibre would otherwise use the
    file name, which carries the ingest suffix)."""
    tmp = pdf_path + ".tmp"
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            raise TagError("PDF is encrypted")
        meta = reader.metadata or {}
        keywords = _split_keywords(str(meta.get("/Keywords") or ""))
        if owner_tag in keywords:
            return
        writer = PdfWriter(clone_from=reader)
        new = {"/Keywords": ", ".join(keywords + [owner_tag])}
        if title and not str(meta.get("/Title") or "").strip():
            new["/Title"] = title
        writer.add_metadata(new)
        _merge_xmp_subject(writer, owner_tag)
        with open(tmp, "wb") as f:
            writer.write(f)
    except (PyPdfError, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise TagError(f"cannot rewrite PDF ({e.__class__.__name__}: {str(e)[:80]})")
    os.replace(tmp, pdf_path)

RDF_NS, DC_NS_XMP, X_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://purl.org/dc/elements/1.1/", "adobe:ns:meta/"

def _merge_xmp_subject(writer, owner_tag):
    """PDFs from Word/InDesign/LaTeX carry an XMP packet, and Calibre prefers XMP dc:subject over
    the Info /Keywords when both exist - so the owner tag must live in BOTH. Appends an
    <rdf:li> to dc:subject/rdf:Bag (creating them if missing). If the packet cannot be parsed
    it is dropped so the Info dictionary wins; a PDF without XMP is left alone."""
    from pypdf.generic import DecodedStreamObject, NameObject
    root = writer._root_object
    if "/Metadata" not in root:
        return
    try:
        stream = root["/Metadata"].get_object()
        xml = etree.fromstring(stream.get_data(), etree.XMLParser(recover=True, resolve_entities=False, huge_tree=False))
        if xml is None:
            raise ValueError("empty XMP")
        descs = xml.findall(f".//{{{RDF_NS}}}Description")
        if not descs:
            rdf = xml.find(f".//{{{RDF_NS}}}RDF")
            if rdf is None:
                raise ValueError("no rdf:RDF")
            descs = [etree.SubElement(rdf, f"{{{RDF_NS}}}Description", {f"{{{RDF_NS}}}about": ""})]
        subj = next((d.find(f"{{{DC_NS_XMP}}}subject") for d in descs if d.find(f"{{{DC_NS_XMP}}}subject") is not None), None)
        if subj is None:
            subj = etree.SubElement(descs[0], f"{{{DC_NS_XMP}}}subject")
        bag = subj.find(f"{{{RDF_NS}}}Bag")
        if bag is None:
            bag = etree.SubElement(subj, f"{{{RDF_NS}}}Bag")
        if any((li.text or "").strip() == owner_tag for li in bag.findall(f"{{{RDF_NS}}}li")):
            return
        etree.SubElement(bag, f"{{{RDF_NS}}}li").text = owner_tag
        new = DecodedStreamObject()
        new.set_data(etree.tostring(xml, xml_declaration=False, encoding="utf-8"))
        new[NameObject("/Type")] = NameObject("/Metadata")
        new[NameObject("/Subtype")] = NameObject("/XML")
        root[NameObject("/Metadata")] = writer._add_object(new)
    except Exception:
        del root["/Metadata"]          # unreadable XMP: let the Info dictionary carry the tag

CBI_KEY = "ComicBookInfo/1.0"

def add_owner_tag_cbz(cbz_path, owner_tag, title=None):
    """Put the tag in a ComicBookInfo/1.0 block in the archive comment (merged if one exists)."""
    tmp = cbz_path + ".tmp"
    try:
        zin = zipfile.ZipFile(cbz_path, "r")
    except zipfile.BadZipFile as e:
        raise TagError(f"not a zip: {e}")
    with zin:
        try:
            info = json.loads(zin.comment.decode("utf-8")) if zin.comment else {}
            if not isinstance(info, dict) or not isinstance(info.get(CBI_KEY, {}), dict):
                info = {}
        except (ValueError, UnicodeDecodeError):
            info = {}
        cbi = info.setdefault(CBI_KEY, {})
        tags = [t for t in (cbi.get("tags") or []) if isinstance(t, str)]
        if owner_tag in tags:
            return
        cbi["tags"] = tags + [owner_tag]
        if title and not cbi.get("title"):
            cbi["title"] = title
        info.setdefault("appID", "bookstack")
        info["lastModified"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                for zi in zin.infolist():
                    zout.writestr(zi, zin.read(zi))
                zout.comment = json.dumps(info).encode("utf-8")
        except (zipfile.BadZipFile, OSError) as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise TagError(f"cannot rewrite CBZ ({e})")
    os.replace(tmp, cbz_path)
