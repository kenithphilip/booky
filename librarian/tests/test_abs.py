"""Audiobookshelf automation against a scripted fake of the ABS REST API (shapes recorded from
a real ABS 2.36 in tests/e2e_driver.py section 13 and a probe run)."""
import json, zipfile, io, os
import pytest
import requests
import config, db, worker
import abs as absapi
from conftest import make_epub  # noqa: F401  (fixture side effects)

class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text or (json.dumps(body) if body is not None else "")
    def json(self):
        if self._body is None: raise ValueError("no json")
        return self._body

class FakeABS:
    """Enough of ABS's API for our calls; records every request."""
    def __init__(self, init=False):
        self.init, self.calls, self.users, self.libs, self.items, self.keys = init, [], [], [], {}, []
        self.next_id = 1
    def _id(self): self.next_id += 1; return f"id-{self.next_id}"
    def __call__(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = url.split("//", 1)[1].split("/", 1)[1]; path = "/" + path
        self.calls.append((method, path, json, (headers or {}).get("Authorization")))
        auth = (headers or {}).get("Authorization", "")
        if path == "/status": return FakeResp(200, {"isInit": self.init, "serverVersion": "2.36.1"})
        if path == "/init":
            if self.init: return FakeResp(500, text="already")
            self.init = True; self.users.append({"id": "root-id", "username": json["newRoot"]["username"], "type": "root", "itemTagsSelected": [], "permissions": {"accessAllTags": True}})
            return FakeResp(200, text="OK")
        if path == "/login":
            u = next((x for x in self.users if x["username"] == json["username"]), None)
            if not u or json["password"] == "wrong": return FakeResp(401, {"error": "Invalid"})
            return FakeResp(200, {"user": {**u, "token": "legacy-" + u["id"], "accessToken": "jwt-" + u["id"], "refreshToken": ""}})
        if not auth.startswith("Bearer ") or auth == "Bearer ": return FakeResp(401, {"error": "Unauthorized"})
        if path == "/api/api-keys" and method == "POST":
            k = "apikey-" + self._id(); self.keys.append(k); return FakeResp(200, {"apiKey": {"apiKey": k, "id": self._id(), "name": json["name"], "expiresAt": None}})
        if path == "/api/libraries" and method == "GET": return FakeResp(200, {"libraries": self.libs})
        if path == "/api/libraries" and method == "POST":
            lib = {"id": self._id(), "name": json["name"], "folders": json["folders"]}; self.libs.append(lib); return FakeResp(200, lib)
        if path.startswith("/api/libraries/") and path.endswith("/scan"): return FakeResp(200, text="OK")
        if path.startswith("/api/libraries/") and path.endswith("/items"):
            lid = path.split("/")[3]; return FakeResp(200, {"results": [it for it in self.items.values() if it["libraryId"] == lid]})
        if path == "/api/users" and method == "GET": return FakeResp(200, {"users": self.users})
        if path == "/api/users" and method == "POST":
            if any(u["username"] == json["username"] for u in self.users): return FakeResp(400, text="Username already taken")
            u = {"id": self._id(), **{k: v for k, v in json.items() if k != "password"}}; self.users.append(u); return FakeResp(200, {"user": u})
        if path.startswith("/api/users/") and method == "PATCH":
            u = next(x for x in self.users if x["id"] == path.rsplit("/", 1)[1]); u.update({k: v for k, v in json.items() if k != "password"}); u["pw"] = json.get("password", u.get("pw")); return FakeResp(200, {"user": u})
        if path.startswith("/api/users/") and method == "DELETE":
            self.users = [x for x in self.users if x["id"] != path.rsplit("/", 1)[1]]; return FakeResp(200, {"success": True})
        if path.startswith("/api/items/") and path.endswith("/media") and method == "PATCH":
            it = self.items[path.split("/")[3]]; it["media"]["tags"] = json["tags"]; return FakeResp(200, {"updated": True})
        if path.startswith("/api/items/") and method == "GET":
            return FakeResp(200, self.items[path.split("/")[3]])
        return FakeResp(404, text="nope")

@pytest.fixture
def fake(monkeypatch):
    f = FakeABS()
    monkeypatch.setattr(requests, "request", f)
    monkeypatch.setattr(config, "ABS_TOKEN", "")
    return f

def test_bootstrap_creates_root_key_and_library_once(fake):
    out = absapi.bootstrap("root", "rootpass-1234")
    assert out["created_root"] and out["created_library"] and out["api_key"].startswith("apikey-")
    assert [c[1] for c in fake.calls] == ["/status", "/init", "/login", "/api/api-keys", "/api/libraries", "/api/libraries"]
    # the key was minted with the root SESSION token, then used for the library call
    assert fake.calls[3][3] == "Bearer legacy-root-id" and fake.calls[4][3].startswith("Bearer apikey-")
    assert fake.libs[0]["folders"] == [{"fullPath": "/audiobooks"}] and fake.libs[0]["name"] == "Audiobooks"
    again = absapi.bootstrap("root", "rootpass-1234")
    assert not again["created_root"] and not again["created_library"] and again["library_id"] == out["library_id"]
    with pytest.raises(absapi.AbsError):
        absapi.login("root", "wrong")

def test_ensure_user_creates_then_aligns_and_never_touches_root(fake):
    key = absapi.bootstrap("root", "rootpass-1234")["api_key"]; config.ABS_TOKEN = key
    u, how = absapi.ensure_user("alice", "alicepass-1")
    assert how == "created" and u["itemTagsSelected"] == ["owner:alice"] and u["permissions"]["accessAllTags"] is False and u["type"] == "user"
    fake.users[-1]["itemTagsSelected"] = []                      # drifted (admin edited it in the UI)
    u, how = absapi.ensure_user("alice")
    assert how == "updated" and u["itemTagsSelected"] == ["owner:alice"]
    u, how = absapi.ensure_user("root", "x"); assert how == "root" and fake.users[0]["itemTagsSelected"] == []
    with pytest.raises(absapi.AbsError):
        absapi.ensure_user("ghost")                             # no password, does not exist
    assert absapi.set_password("alice", "newpass-1234") and fake.users[-1]["pw"] == "newpass-1234"
    assert absapi.remove_user("alice") is True and absapi.remove_user("alice") is False
    with pytest.raises(absapi.AbsError):
        absapi.remove_user("root")

def test_tag_folder_waits_for_scan_then_tags_and_merges(fake):
    key = absapi.bootstrap("root", "rootpass-1234")["api_key"]; config.ABS_TOKEN = key
    lid = fake.libs[0]["id"]
    sleeps = []
    def appear_after_two(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            fake.items["it1"] = {"id": "it1", "libraryId": lid, "relPath": "alice - My Book", "path": "/audiobooks/alice - My Book", "media": {"tags": ["Fiction"]}}
    note = absapi.tag_folder("alice - My Book", "alice", attempts=5, delay=1, sleep=appear_after_two)
    assert note == "tagged owner:alice in ABS" and fake.items["it1"]["media"]["tags"] == ["Fiction", "owner:alice"]
    assert absapi.tag_item("it1", "owner:alice") is False            # idempotent
    note = absapi.tag_folder("never here", "bob", attempts=2, delay=1, sleep=lambda s: None)
    assert note.startswith("ABS did not index") and "owner:bob" in note
    config.ABS_TOKEN = ""
    assert absapi.tag_folder("x", "bob", attempts=1, sleep=lambda s: None) == "set tag owner:bob in ABS (no API token)"
    assert "skipped" in absapi.trigger_scan()

def test_worker_queues_a_persistent_tag_job_and_keeps_the_request_open(fake, tmp_path, monkeypatch):
    key = absapi.bootstrap("root", "rootpass-1234")["api_key"]; monkeypatch.setattr(config, "ABS_TOKEN", key)
    z = tmp_path / "great audiobook.zip"
    with zipfile.ZipFile(z, "w") as zf: zf.writestr("01.mp3", b"ID3"); zf.writestr("cover.jpg", b"jpg")
    rid = db.add("alice", {"kind": "audio", "source": "dropbox", "title": "x", "download_url": "local"}, status="importing")
    note = worker.ingest_local_file(str(z), "alice", rid)
    assert note == "ABS scan triggered; waiting for Audiobookshelf to index it to tag owner:alice" and worker._status_for(note) == "tagging"
    assert [(j["folder"], j["owner"], j["rid"]) for j in db.pending_tag_jobs()] == [("alice - great audiobook", "alice", rid)]
    assert sorted(os.listdir(os.path.join(config.AUDIO_DIR, "alice - great audiobook"))) == ["01.mp3", "cover.jpg"]

def _tagging_request(folder="alice - My Book"):
    rid = db.add("alice", {"kind": "audio", "source": "librivox", "title": "My Book", "download_url": "https://x"}, status="tagging")
    db.set_status(rid, "tagging", f"ABS scan triggered; {worker.TAG_WAIT_NOTE} owner:alice")
    db.add_tag_job(rid, folder, "alice", now=1000.0)
    return rid

def test_tag_jobs_survive_slow_scans_and_restarts_then_finish_the_request(fake, monkeypatch):
    """F22: the tag used to be a 2-minute daemon thread; a slow ABS scan or a restart left the
    audiobook untagged (invisible to its owner) while the request already said 'done'."""
    key = absapi.bootstrap("root", "rootpass-1234")["api_key"]; monkeypatch.setattr(config, "ABS_TOKEN", key)
    sent = []
    monkeypatch.setattr(worker.notify, "send", lambda ev, r: sent.append((ev, r["status"])))
    rid = _tagging_request()
    now = 1000.0
    for _ in range(10):                                    # ABS takes a long time to index it
        assert worker.process_tag_jobs(now) == 0
        now += 400
    assert db.get(rid)["status"] == "tagging" and sent == [] and db.pending_tag_jobs()[0]["attempts"] == 10
    db.init(); db.recover_on_start()                       # a portal restart does not lose or break the job
    assert db.get(rid)["status"] == "tagging" and len(db.pending_tag_jobs()) == 1
    lid = fake.libs[0]["id"]
    fake.items["it1"] = {"id": "it1", "libraryId": lid, "relPath": "alice - My Book", "path": "/audiobooks/alice - My Book", "media": {"tags": ["Fiction"]}}
    # J04: the first pass tags what ABS has indexed so far; the job closes only when the item
    # count is stable across two passes (a box set is indexed book by book)
    assert worker.process_tag_jobs(now) == 0 and db.get(rid)["status"] == "tagging"
    assert fake.items["it1"]["media"]["tags"] == ["Fiction", "owner:alice"]
    assert worker.process_tag_jobs(now + 20) == 1
    r = db.get(rid)
    assert r["status"] == "done" and r["detail"] == "ABS scan triggered; tagged owner:alice in ABS" and sent == [("done", "done")]
    assert db.pending_tag_jobs() == [] and worker.process_tag_jobs(now + 999) == 0

def test_tag_job_errors_are_visible_and_giving_up_alerts_the_admin(fake, monkeypatch):
    monkeypatch.setattr(config, "ABS_TOKEN", "k")
    monkeypatch.setattr(absapi, "find_items_by_folder", lambda folder: (_ for _ in ()).throw(requests.ConnectionError("ABS down")))
    alerts = []
    monkeypatch.setattr(worker.notify, "alert", lambda title, text, prio="default": alerts.append((title, text, prio)) or ["webhook"])
    monkeypatch.setattr(worker.notify, "send", lambda ev, r: None)
    rid = _tagging_request()
    worker.process_tag_jobs(1000.0)
    r = db.get(rid)
    assert r["status"] == "tagging" and "last ABS error: ConnectionError: ABS down" in r["detail"]
    assert db.pending_tag_jobs()[0]["next_try"] == 1015.0                 # backoff, not a hot loop
    assert worker.process_tag_jobs(1001.0) == 0 and db.pending_tag_jobs()[0]["attempts"] == 1   # not due yet
    worker.process_tag_jobs(1000.0 + config.ABS_TAG_GIVE_UP_HOURS * 3600 + 1)
    r = db.get(rid)
    assert r["status"] == "needs-tag" and "set tag owner:alice in ABS by hand" in r["detail"] and "ABS down" in r["detail"]
    assert alerts and alerts[0][0] == "audiobook left untagged" and alerts[0][2] == "high" and db.pending_tag_jobs() == []

def test_tag_jobs_without_a_token_go_to_the_admin(fake, monkeypatch):
    rid = _tagging_request()
    monkeypatch.setattr(worker.notify, "send", lambda ev, r: None)
    worker.process_tag_jobs(2000.0)
    assert db.get(rid)["status"] == "needs-tag" and db.pending_tag_jobs() == []

def test_zip_extraction_refuses_traversal_and_bombs(tmp_path):
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf: zf.writestr("../../etc/evil.mp3", b"x")
    with zipfile.ZipFile(io.BytesIO(bad.getvalue())) as zf, pytest.raises(ValueError):
        worker._safe_extract(zf, str(tmp_path / "out"))
    assert not (tmp_path / "etc").exists()
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(worker.MAX_ZIP_MEMBERS + 1): zf.writestr(f"{i}.mp3", b"x")
    with zipfile.ZipFile(io.BytesIO(many.getvalue())) as zf, pytest.raises(ValueError):
        worker._safe_extract(zf, str(tmp_path / "out2"))
    ok = io.BytesIO()
    with zipfile.ZipFile(ok, "w") as zf: zf.writestr("Disc 1/01.mp3", b"x"); zf.writestr("Disc 1/", b"")
    with zipfile.ZipFile(io.BytesIO(ok.getvalue())) as zf:
        worker._safe_extract(zf, str(tmp_path / "out3"))
    assert os.listdir(tmp_path / "out3" / "Disc 1") == ["01.mp3"]

def test_cli_roundtrip(fake, capsys):
    assert absapi._cli(["init", "--password", "rootpass-1234"]) == 0
    out = json.loads(capsys.readouterr().out); assert out["ok"] and out["api_key"] and out["library_id"]
    config.ABS_TOKEN = out["api_key"]
    assert absapi._cli(["ensure-user", "alice", "--password", "alicepass-1"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "created"
    assert absapi._cli(["list-users"]) == 0
    users = json.loads(capsys.readouterr().out)
    assert {u["username"]: u["isolated"] for u in users} == {"root": False, "alice": True}
    assert absapi._cli(["ensure-user", "ghost"]) == 2 and "does not exist" in capsys.readouterr().err
    assert absapi._cli(["scan"]) == 0 and "triggered" in capsys.readouterr().out

def test_cli_reads_passwords_from_stdin(fake, capsys, monkeypatch):
    import sys, io
    monkeypatch.setattr(sys, "stdin", io.StringIO("rootpass-1234\n"))
    assert absapi._cli(["init", "--password-stdin"]) == 0
    out = json.loads(capsys.readouterr().out); config.ABS_TOKEN = out["api_key"]
    assert fake.users[0]["username"] == "root"
    monkeypatch.setattr(sys, "stdin", io.StringIO("alicepass-1"))
    assert absapi._cli(["ensure-user", "alice", "--password-stdin"]) == 0 and json.loads(capsys.readouterr().out)["result"] == "created"
    monkeypatch.setattr(sys, "stdin", io.StringIO("newpass-1234\n"))
    assert absapi._cli(["passwd", "alice", "--password-stdin"]) == 0 and fake.users[-1]["pw"] == "newpass-1234"
    assert absapi._cli(["ensure-user", "alice"]) == 0                                # still optional for ensure-user
    for bad in (["init"], ["passwd", "alice"], ["passwd", "alice", "--password", "x", "--password-stdin"]):
        with pytest.raises(SystemExit):
            absapi._cli(bad)
