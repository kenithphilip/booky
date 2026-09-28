"""Build-time fallback for `pip install -r requirements.lock` (librarian/Dockerfile).

Measured on the real server 2026-09-28: pypi.org's CDN kept timing out on pip's JSON request for
ONE project page (/simple/click/) while the same page as plain HTML answered in milliseconds,
from the host and from a container, and every other package downloaded fine. pip always asks for
JSON first, so the build failed every time and so did every Deploy.

Every requirement is pinned to exact sha256 hashes, so WHERE a file comes from does not matter:
this fetches each project's plain HTML page (what answered), picks the pinned file that suits this
interpreter (pip's own tag logic, from its vendored `packaging`), downloads it into a directory and
checks its hash. pip then installs with --no-index --find-links from that directory and
--require-hashes, verifying every file again. Standard library only (plus pip's vendored packaging).
"""
import hashlib, os, re, sys, time, urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import parse_wheel_filename

INDEX = os.environ.get("WHEELS_INDEX", "https://pypi.org/simple/")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def pins(path):
    """{project: {sha256, ...}} from a pip-compile --generate-hashes file."""
    out, cur = {}, None
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9_.-]+)==\S+", line)
        if m:
            cur = re.sub(r"[-_.]+", "-", m.group(1)).lower()
            out[cur] = set()
        for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if cur:
                out[cur].add(h)
    return out


def get(url, tries=6):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "text/html"}), timeout=60).read()
        except OSError as e:
            if i == tries - 1:
                raise
            print(f"  retrying {url} ({e})", flush=True)
            time.sleep(3 * (i + 1))


def pick(files):
    """[(filename, url, sha)] -> the one to install here: the most specific compatible wheel,
    else the sdist."""
    order = {t: i for i, t in enumerate(sys_tags())}
    best = None
    for name, url, sha in files:
        if name.endswith(".whl"):
            try:
                tags = parse_wheel_filename(name)[3]
            except Exception:
                continue
            rank = min((order[t] for t in tags if t in order), default=None)
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, name, url, sha)
    if best:
        return best[1:]
    sdists = [f for f in files if f[0].endswith((".tar.gz", ".zip"))]
    return sdists[0] if sdists else None


def main(lock, dest):
    os.makedirs(dest, exist_ok=True)
    for project, hashes in pins(lock).items():
        page = urljoin(INDEX, project + "/")
        parser = Links()
        parser.feed(get(page).decode("utf-8", "replace"))
        files = []
        for href in parser.hrefs:
            url, frag = urldefrag(urljoin(page, href))
            m = re.match(r"sha256=([0-9a-f]{64})", frag or "")
            if m and m.group(1) in hashes:
                files.append((url.rsplit("/", 1)[-1], url, m.group(1)))
        chosen = pick(files)
        if not chosen:
            sys.exit(f"{project}: no pinned file found on {page}")
        name, url, sha = chosen
        data = get(url)
        if hashlib.sha256(data).hexdigest() != sha:
            sys.exit(f"{project}: {name} does not match its pinned hash")
        open(os.path.join(dest, name), "wb").write(data)
        print(f"  {name}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
