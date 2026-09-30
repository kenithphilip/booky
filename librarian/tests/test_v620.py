"""v6.2.0: readers choose the devices they read on (on the start page); comics are made for those
screens; every comic's pages are laid out from their shapes (spreads, landscape books, webtoons,
newspaper dailies), with a per-comic choice; the host job runs KCC accordingly."""
import json, os, subprocess, zipfile
import pytest
import config, db, cwa, comics, devicemodels, admin_cli
from conftest import login, post
from test_comics import _comic_book, _png


@pytest.fixture(autouse=True)
def _no_shape_cache():
    comics._SHAPES.clear()


def _cbz(path, shapes):
    """A CBZ whose pages have these (w, h) sizes (tiny PNGs: only the header matters)."""
    with zipfile.ZipFile(path, "w") as z:
        for i, (w, h) in enumerate(shapes):
            z.writestr(f"{i + 1:03d}.png", _png(w, h))
    return str(path)


def _book_file(book_id, shapes):
    rel = comics.comic_books()[book_id]["rel"]
    _cbz(os.path.join(config.LIBRARY_DIR, rel), shapes)
    return rel


PORTRAIT, SPREAD, WIDE, TALL, DAILY = (60, 90), (120, 90), (135, 100), (60, 200), (170, 50)


# ---- the device catalogue ------------------------------------------------------------------------
def test_only_known_devices_are_kept_in_catalogue_order():
    assert devicemodels.clean(["ipad", "nonsense", "kobo-sage", "ipad"]) == ["kobo-sage", "ipad"]
    assert all(m["profile"] is None or m["profile"] in devicemodels.SCREEN for m in devicemodels.BY_KEY.values())

def test_the_kobo_copy_is_made_for_the_sharpest_kobo_and_in_colour_when_any_reader_has_colour(users):
    assert devicemodels.kobo_target([]) == (None, True, []), "nobody chose: as before (configured, colour)"
    db.set_devices("bob", ["kobo-clara-bw", "iphone"])
    assert devicemodels.kobo_target(["bob"])[:2] == ("KoC", False), "a black-and-white Kobo: greyscale"
    db.set_devices("alice", ["kobo-libra-colour"])
    assert devicemodels.kobo_target(["bob", "alice"])[:2] == ("KoLC", True)
    db.set_devices("alice", ["kobo-clara-colour"])
    db.set_devices("bob", ["kobo-sage"])
    assert devicemodels.kobo_target(["bob", "alice"])[:2] == ("KoS", True), "the Sage's screen, in colour"
    db.set_devices("alice", ["ipad"])            # alice's Kobo syncs but she chose no Kobo model
    assert devicemodels.kobo_target(["bob", "alice"])[:2] == ("KoS", True), "the unknown Kobo counts as the default, in colour"
    db.set_devices("bob", ["kobo-clara-bw"])
    assert devicemodels.kobo_target(["bob", "alice"])[:2] == (None, True), "the default screen is sharper than a Clara"

def test_a_comic_for_a_kindle_is_made_for_that_kindle(users):
    assert devicemodels.kindle_target("bob") == (None, True, None)
    db.set_devices("bob", ["kindle-paperwhite-2021"])
    assert devicemodels.kindle_target("bob") == ("KPW5", False, "Kindle Paperwhite (2021) / Signature Edition")
    db.set_devices("bob", ["kindle-colorsoft"])
    assert devicemodels.kindle_target("bob")[:2] == ("KCS", True)

def test_a_reader_who_ticked_a_kobo_gets_kobo_copies_before_its_first_sync(users, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    assert not comics.uses_kobo("bob")
    db.set_devices("bob", ["kobo-libra-2"])
    assert comics.uses_kobo("bob")


# ---- the start page ------------------------------------------------------------------------------
def test_the_start_page_saves_the_devices_and_shows_what_to_do_on_each(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    login(client, "bob", "bobpass1")
    html = client.get("/hub").get_data(as_text=True)
    assert "Which do you read on?" in html and 'value="kobo-libra-colour"' in html and "Kindle address (if you have one)" in html
    html = post(client, "/hub", device=["kobo-clara-bw", "ipad", "bogus"]).get_data(as_text=True)
    assert db.get_prefs("bob")["devices"] == ["kobo-clara-bw", "ipad"]
    assert "Saved: Kobo Clara BW / Clara 2E / Clara HD, iPad." in html
    assert "Kobo copies made for its screen" in html and "Panels (iPhone and iPad) or Chunky" in html and "/opds/" in html
    assert "Kobo linked" in html and "Kindle address" not in html, "only the steps for the devices chosen"
    post(client, "/hub")                                       # nothing ticked: cleared
    assert db.get_prefs("bob")["devices"] == []
    assert client.post("/hub", data={"device": ["kobo-sage"]}).status_code == 400, "no form token: refused"
    assert db.get_prefs("bob")["devices"] == []

def test_the_devices_page_shows_them_and_changes_them_too(client):
    login(client, "bob", "bobpass1")
    assert "comics are made for a Kobo Libra Colour and a Kindle Colorsoft, in colour" in client.get("/devices").get_data(as_text=True)
    r = post(client, "/hub", device=["kindle-basic", "android-phone"], back="devices")
    html = r.get_data(as_text=True)
    assert r.request.path == "/devices" and "Kindle (2022 / 2024) · Android phone" in html

def test_the_phone_and_tablet_guide_names_the_apps_for_each_platform(client):
    login(client, "bob", "bobpass1")
    html = client.get("/help/phone-tablet").get_data(as_text=True)
    assert "iPhone and iPad" in html and "Android phone and tablet" in html and "CDisplayEx" in html and "ShelfPlayer" in html


# ---- page shapes -----------------------------------------------------------------------------------
@pytest.mark.parametrize("pages, layout", [
    ([PORTRAIT] * 20, "portrait"),
    ([PORTRAIT] * 18 + [SPREAD, SPREAD], "spreads"),
    ([PORTRAIT] + [WIDE] * 19, "rotate"),                 # The Complete Peanuts: a portrait cover, wide pages
    ([TALL] * 10, "strip"),
    ([DAILY] * 10, "dailies"),
    ([PORTRAIT] + [DAILY] * 10, "rotate"),                # a portrait cover: maximizestrips would cut it
    ([(90, 90)] * 10, "portrait"),                        # square pages: nothing to cut or turn
    ([(115, 100)] * 10, "portrait"),                      # 1.15: not wide by KCC's measure (> 1.16)
])
def test_the_layout_follows_the_shape_of_the_pages(tmp_path, pages, layout):
    assert comics.auto_layout(comics.page_shapes(_cbz(tmp_path / "c.cbz", pages))) == layout

def test_one_spread_deep_in_a_long_book_is_found(tmp_path):
    shapes = comics.page_shapes(_cbz(tmp_path / "c.cbz", [PORTRAIT] * 199 + [SPREAD] + [PORTRAIT] * 50))
    assert shapes["seen"] == 250 and shapes["wide"] == 1 and comics.auto_layout(shapes) == "spreads"

def test_a_small_scan_is_enlarged_by_kcc_a_large_one_is_not(tmp_path):
    small = comics.page_shapes(_cbz(tmp_path / "s.cbz", [(600, 900)] * 10))
    large = comics.page_shapes(_cbz(tmp_path / "l.cbz", [(1300, 1900)] * 10))
    assert comics.wants_upscale(small, "KoLC", "portrait") and not comics.wants_upscale(large, "KoLC", "portrait")
    assert not comics.wants_upscale(small, "KoLC", "strip"), "never for a webtoon"
    assert comics.wants_upscale(large, "KS3", "portrait"), "a Kindle Scribe's screen is larger"


# ---- what the host job is told -----------------------------------------------------------------------
def test_the_kobo_copy_row_carries_the_layout_and_the_readers_kobo(users, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    _comic_book(1, "Saga", 1, ["Comics", "owner:bob"])
    _book_file(1, [PORTRAIT] * 18 + [SPREAD] * 2)
    assert comics.kobo_due() == [], "nobody reads on a Kobo"
    db.set_devices("bob", ["kobo-clara-bw"])
    (row,) = comics.kobo_due()
    assert (row["layout"], row["profile"], row["colour"], row["upscale"]) == ("spreads", "KoC", False, True)
    assert row["strip"] is False and row["landscape"] is False and "owners" not in row

def test_the_kindle_row_is_made_for_the_senders_kindle(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    cwa.set_kindle_mail("bob", "bob@kindle.com")
    db.set_devices("bob", ["kindle-paperwhite-2024"])
    _comic_book(9, "The Complete Peanuts", 1, ["Comics", "owner:bob"])
    _book_file(9, [PORTRAIT] + [WIDE] * 11)
    login(client, "bob", "bobpass1")
    post(client, "/kindle/9")
    (row,) = comics.kindle_due()
    assert (row["layout"], row["profile"], row["colour"], row["landscape"]) == ("rotate", "KPW6", False, True)

def test_a_reader_can_choose_the_layout_and_the_kobo_copy_is_made_again(client, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    db.set_devices("bob", ["kobo-libra-colour"])
    _comic_book(1, "Saga", 1, ["Comics", "owner:bob"], formats=("cbz", "kepub"))
    _book_file(1, [PORTRAIT] * 18 + [SPREAD] * 2)
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "Pages on e-readers: Two-page spreads (chosen from its pages)" in html
    html = post(client, "/book/1/layout", layout="split").get_data(as_text=True)
    assert "being made again" in html and db.comic_layout_get(1) == "split"
    (row,) = comics.kobo_due()
    assert row["layout"] == "split" and row["layout_chosen"] is True and row["remake"] is True
    post(client, "/book/1/layout", layout="auto")
    assert db.comic_layout_get(1) is None and comics.kobo_due()[0]["layout"] == "spreads"
    assert post(client, "/book/1/layout", layout="-r 9").status_code == 400
    _comic_book(2, "Saga", 2, ["Comics", "owner:alice"])
    assert post(client, "/book/2/layout", layout="rotate").status_code == 404, "not bob's comic"

def test_choosing_a_layout_makes_no_kobo_copy_for_a_comic_nobody_reads_on_a_kobo(client, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    _comic_book(1, "Saga", 1, ["Comics", "owner:bob"])
    _book_file(1, [PORTRAIT] * 10)
    login(client, "bob", "bobpass1")
    post(client, "/book/1/layout", layout="rotate")
    assert db.comic_layout_get(1) == "rotate" and db.comic_convert_state([1]) == {}

def test_what_the_copy_was_made_for_is_kept_and_a_changed_kobo_is_noticed(client, monkeypatch, capsys):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    db.set_devices("bob", ["kobo-clara-bw"])
    _comic_book(1, "Saga", 1, ["Comics", "owner:bob"], formats=("cbz", "kepub"))
    _book_file(1, [PORTRAIT] * 10)
    admin_cli.main(["comics", "kobo-result", "1", "ok", "--made",
                    json.dumps({"profile": "KoC", "colour": False, "layout": "portrait", "upscale": False, "x": "<script>"})])
    capsys.readouterr()
    assert json.loads(db.comic_convert_state([1])[1]["made"]) == {"profile": "KoC", "colour": False, "layout": "portrait", "upscale": False}
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "Kobo copy ready (greyscale, fixed layout, made for a Kobo Clara BW" in html and "call for a different copy" not in html
    db.set_devices("bob", ["kobo-libra-colour"])
    assert "call for a different copy now" in client.get("/book/1").get_data(as_text=True)
    admin_cli.main(["comics", "kobo-result", "1", "ok", "--made", "not json"])
    capsys.readouterr()
    assert db.comic_convert_state([1])[1]["status"] == "done"


# ---- the host job (scripts/comic-convert.sh) with a stand-in for docker ----------------------------------
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
log = os.environ["FAKE_LOG"]
def note(kind, args):
    with open(log, "a") as f:
        f.write(json.dumps([kind, args]) + "\n")
if a[:1] == ["run"]:
    note("kcc", a)
    out = next(v.split(":")[0] for v in a if v.endswith(":/out"))
    open(os.path.join(out, "comic.epub"), "wb").write(b"PK fake")
elif "admin_cli" in a:
    i = a.index("admin_cli")
    cmd = a[i + 1:]
    note("admin", cmd)
    if cmd[:2] == ["comics", "kobo-due"]:
        print(json.dumps({"ok": True, "rows": json.loads(os.environ["FAKE_ROWS"])}))
    elif cmd[:2] == ["comics", "kindle-due"]:
        print(json.dumps({"ok": True, "rows": json.loads(os.environ.get("FAKE_KROWS") or "[]")}))
    else:
        print(json.dumps({"ok": True, "status": "done"}))
elif "calibredb" in " ".join(a) and "list" in a:
    print(json.dumps([{"tags": ["Comics", "owner:bob"], "formats": ["/x/a.cbz", "/x/a.kepub"]}]))
'''

def _host_run(tmp_path, rows, krows=()):
    stack = tmp_path / "stack"
    books = stack / "library" / "books" / "A" / "Saga (1)"
    books.mkdir(parents=True)
    (books / "Saga.cbz").write_bytes(b"PK")
    (stack / ".env").write_text("COMICS_ENABLED=true\n")
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "docker").chmod(0o755)
    log = tmp_path / "log"
    for r in rows:
        r.setdefault("calibre_id", 1); r.setdefault("rel", "A/Saga (1)/Saga.cbz"); r.setdefault("title", "Saga")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", STACK_DIR=str(stack), FAKE_LOG=str(log),
               FAKE_ROWS=json.dumps(rows), FAKE_KROWS=json.dumps(list(krows)), COMIC_ALERT="/bin/true", COMIC_METAPUSH_LOCK=str(tmp_path / "lock"))
    r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    kcc = [c[1] for c in calls if c[0] == "kcc"]
    results = [c[1] for c in calls if c[0] == "admin" and c[1][:2] == ["comics", "kobo-result"]]
    return kcc, results

SCRIPT = "/scripts/comic-convert.sh" if os.path.exists("/scripts/comic-convert.sh") else \
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "comic-convert.sh")

def _kcc_opts(args):
    return args[args.index("ghcr.io/ciromattia/kcc:v12.0.0") + 1:]

@pytest.mark.parametrize("row, want, unwanted", [
    ({"layout": "spreads", "profile": "KoC", "colour": False, "upscale": True, "kind": "manga"},
     ["-p", "KoC", "-m", "-r", "2", "-c", "0", "-u"], ["--forcecolor"]),
    ({"layout": "rotate", "profile": "KoLC", "colour": True}, ["-p", "KoLC", "--forcecolor", "-r", "1"], ["-u", "-w"]),
    ({"layout": "strip", "upscale": True}, ["-p", "KoLC", "--forcecolor", "-w"], ["-u", "-r"]),
    ({"layout": "dailies"}, ["--maximizestrips"], ["-r"]),
    ({"layout": "portrait"}, ["-p", "KoLC", "--forcecolor"], ["-r", "-w", "-c", "-u"]),
    ({"strip": False, "landscape": True}, ["-r", "1", "--forcecolor"], []),          # a portal from before v6.2
    ({"layout": "-o /etc", "profile": "--delete", "colour": "no"}, ["-p", "KoLC", "--forcecolor"], ["--delete", "-o /etc"]),
])
def test_the_host_job_runs_kcc_as_the_portal_says_and_nothing_else(tmp_path, row, want, unwanted):
    kcc, results = _host_run(tmp_path, [dict(row)])
    (args,) = kcc
    opts = _kcc_opts(args)
    joined = " " + " ".join(opts) + " "
    assert " " + " ".join(want) + " " in joined or all(w in opts for w in want), opts
    for u in unwanted:
        assert u not in opts, (u, opts)
    assert "--network" in args and "none" in args and opts[-1] == "/in/comic.cbz"
    (res,) = results
    made = json.loads(res[res.index("--made") + 1])
    assert made["layout"] in comics.LAYOUTS and res[3] == "ok"

@pytest.mark.parametrize("layout, want", [("rotate", ["-r", "1", "--norotate"]), ("dailies", ["-r", "1", "--norotate"]),
                                          ("spreads", ["-r", "2", "-c", "0"])])
def test_on_a_kindle_wide_pages_stay_whole_and_upright(tmp_path, layout, want):
    """KCC's Send-to-Kindle format frames every page alike: a page turned sideways in a landscape
    book's frame came out a narrow strip (tests/kcc-layout-test.sh measures it with the real KCC)."""
    krow = {"job": 7, "calibre_id": 1, "rel": "A/Saga (1)/Saga.cbz", "title": "Saga", "max_mb": 45,
            "layout": layout, "profile": "KPW5", "colour": False, "upscale": False}
    kcc, _ = _host_run(tmp_path, [], [krow])
    opts = _kcc_opts(kcc[0])
    assert " ".join(want) in " ".join(opts) and "--forcecolor" not in opts and opts[:2] == ["-p", "KPW5"]
    assert "KFX" in opts and "--maximizestrips" not in opts
