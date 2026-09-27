"""Admin console back end for the whiptail TUI.

The portal's /admin page is a read-mostly dashboard by design; bookstack.sh over Tailscale SSH
is the admin console. The three things the console could not reach at all live here:

  * a portal login lockout (it could be seen nowhere and cleared nowhere),
  * the request queue past the newest 200 rows (an approval or a failure older than that was
    unreachable from every console),
  * files parked in dropbox/<user>/.failed/ (SSH plus mv/rm was the only route).

Every arm prints ONE line of JSON on stdout and exits 0; a failure prints
{"ok": false, "error": "..."} and exits non-zero. bookstack.sh calls it as
    docker exec -i librarian python -m admin_cli <arm> ...
"""
import argparse, base64, binascii, json, os, shutil, sys, time
import config, db

# ---- parked files ----------------------------------------------------------------------
PARKED = ".failed"

def _dropbox_root():
    return os.path.realpath(config.DROPBOX_DIR)

def token_for(path):
    """An opaque, path-safe id for a parked file: base64url of its path relative to the
    dropbox root. Unpadded, so it never needs quoting in a shell or a whiptail menu."""
    rel = os.path.relpath(os.path.realpath(path), _dropbox_root())
    return base64.urlsafe_b64encode(rel.encode("utf-8")).decode("ascii").rstrip("=")

def resolve_token(token):
    """The absolute path a token names, or an error. A token is attacker-reachable only via
    the admin, but it is still decoded into a path: it must not be able to escape the dropbox
    root (nor name a live dropbox file rather than a parked one), so the decoded path is
    checked for '..' AND the resolved result is required to stay under the root and inside a
    .failed/ directory. Symlinks are refused rather than followed."""
    try:
        pad = "=" * (-len(token) % 4)
        rel = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("not a valid parked-file id")
    if not rel or rel.startswith("/") or os.path.isabs(rel) or ".." in rel.split("/"):
        raise ValueError("not a valid parked-file id")
    root = _dropbox_root()
    raw = os.path.join(root, rel)
    if os.path.islink(raw):          # never follow a link planted under .failed/
        raise ValueError("that parked entry is a symbolic link and is not touched")
    p = os.path.realpath(raw)
    if not p.startswith(root + os.sep):
        raise ValueError("that id does not point inside the dropbox folder")
    parts = os.path.relpath(p, root).split(os.sep)
    if len(parts) < 3 or parts[1] != PARKED:
        raise ValueError("that id is not a parked file (dropbox/<user>/.failed/<name>)")
    if not os.path.lexists(p):
        raise ValueError("no such parked file")
    return p

def _reason_for(user, name):
    """Why the worker parked it: its request row's detail, with the '(moved to ...)' tail that
    _park() appends stripped off."""
    for detail in db.detail_like(f"{PARKED}/{name}", limit=1):
        return (detail or "").split(" (moved to")[0].strip()
    return ""

def parked_rows():
    root = _dropbox_root()
    rows = []
    try:
        users = sorted(os.listdir(root))
    except OSError:
        return rows
    for user in users:
        d = os.path.join(root, user, PARKED)
        if user.startswith(".") or not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            size = st.st_size
            if os.path.isdir(p) and not os.path.islink(p):
                size = 0
                for r, _ds, ns in os.walk(p):
                    for n in ns:
                        try:
                            size += os.lstat(os.path.join(r, n)).st_size
                        except OSError:
                            pass
            rows.append({"token": token_for(p), "user": user, "name": name,
                         "bytes": size, "mtime": int(st.st_mtime), "reason": _reason_for(user, name)})
    return rows

def parked_retry(token):
    """Move it back up one level, out of .failed/, so the dropbox watcher picks it up again.
    _decide() lets a file through when the previous row was parked, so this really is a retry."""
    p = resolve_token(token)
    box = os.path.dirname(os.path.dirname(p))
    dst = os.path.join(box, os.path.basename(p))
    if os.path.lexists(dst):
        raise ValueError(f"'{os.path.basename(p)}' is already waiting in that dropbox; "
                         f"deal with that one first")
    shutil.move(p, dst)
    return dst

def parked_delete(token):
    p = resolve_token(token)
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    else:
        os.remove(p)

# ---- arms ------------------------------------------------------------------------------
def _lockout(args):
    if args.what == "status":
        users, ips = db.locked_keys()
        return {"ok": True, "users": users, "ips": ips}
    n = db.clear_login_failures_for(user=args.user, ip=args.ip, everything=bool(args.all))
    db.audit("lockout_cleared", args.user, "tui",
             f"{n} key(s) released" + (f" for {args.ip}" if args.ip else "") + (" (all)" if args.all else ""))
    return {"ok": True, "cleared": n}

def _requests(args):
    if args.what == "list":
        return {"ok": True, "total": db.count_requests(args.status),
                "rows": [{"rid": r["id"], "user": r["owner"], "title": r["title"],
                          "status": r["status"], "detail": r.get("detail") or "",
                          "created": int(r.get("created") or 0)}
                         for r in db.list_requests(args.status, args.limit, args.offset)]}
    rec = db.get(args.rid)
    if not rec:
        raise ValueError(f"no request #{args.rid}")
    if args.what == "retry":
        if rec["status"] != "error" or not rec.get("download_url") or rec["download_url"] == "local":
            raise ValueError("that request has no re-fetchable source (an upload, a dropbox file "
                             "or an e-mailed attachment cannot be downloaded again)")
        db.requeue(args.rid, "requeued from the admin tools")
        db.audit("retry", rec["owner"], "tui", f"#{args.rid} {rec['title']}")
        return {"ok": True}
    # dismiss: 'pending' included on purpose — a removed user's request is stranded otherwise
    if rec["status"] not in ("error", "needs-tag", "pending"):
        raise ValueError(f"a '{rec['status']}' request cannot be dismissed")
    db.set_status(args.rid, "dismissed", "dismissed from the admin tools")
    db.audit("dismiss", rec["owner"], "tui", f"#{args.rid} {rec['title']} ({rec['status']})")
    return {"ok": True}

def _parked(args):
    if args.what == "list":
        return {"ok": True, "rows": parked_rows()}
    if args.what == "retry":
        moved = parked_retry(args.token)
        db.audit("parked_retry", None, "tui", os.path.basename(moved))
        return {"ok": True, "moved": moved}
    parked_delete(args.token)
    db.audit("parked_delete", None, "tui", args.token[:80])
    return {"ok": True}

def _pushes(args):
    """The host side of the Calibre metadata push (scripts/metadata-push.sh). The portal cannot
    write metadata.db itself — it mounts the library read-only and has no Docker socket, both
    on purpose — so it hands the host a list and hears back what happened."""
    if args.what == "pending":
        return {"ok": True, "rows": db.pending_pushes(args.limit)}
    st = db.push_result(args.push_id, args.outcome == "ok", error=args.reason)
    return {"ok": True, "status": st}

def _tags(args):
    """L10's host side: owner tags the portal needs added in Calibre (the portal cannot write
    metadata.db itself — read-only mount, no Docker socket, on purpose)."""
    if args.what == "pending":
        return {"ok": True, "rows": db.pending_tag_pushes(args.limit)}
    row = db.tag_push_result(args.push_id, args.outcome == "ok", error=args.reason)
    if args.outcome == "ok" and row.get("rid"):
        rec = db.get(row["rid"])
        if rec and rec["status"] == "needs-tag":
            db.set_status(row["rid"], "done", f"tagged owner:{row['owner']} in Calibre by the host job")
            import notify
            notify.send("done", db.get(row["rid"]))
    db.audit("tag_push", None, "host", f"#{args.push_id} {args.outcome} {args.reason[:120]}")
    return {"ok": True, "status": row["status"]}

def _converts(args):
    """On-demand conversions for the host job (the portal has no Calibre and no Docker socket)."""
    if args.what == "pending":
        return {"ok": True, "rows": db.pending_converts(args.limit)}
    row = db.convert_result(args.job_id, args.outcome == "ok", args.reason)
    db.audit("convert_result", None, "host", f"#{args.job_id} {args.outcome} {args.reason[:120]}")
    return {"ok": True, "status": row["status"]}

def _catalogs(args):
    """The admin's own OPDS catalogs (catalogs.py). The password arrives on stdin, never argv."""
    import catalogs
    if args.what == "list":
        return {"ok": True, "rows": [{k: c[k] for k in ("id", "source", "name", "url", "user", "enabled", "legacy")}
                                     for c in catalogs.all_catalogs(include_disabled=True)]}
    if args.what in ("add", "test"):
        pw = sys.stdin.read().rstrip("\n") if args.password_stdin else ""
        if args.what == "test":
            ok, why = catalogs.test(args.url, args.user or "", pw)
            return {"ok": ok, "detail": why} if ok else {"ok": False, "error": why}
        bad = catalogs.validate(args.id, args.name, args.url)
        if bad:
            raise ValueError(bad)
        ok, why = catalogs.test(args.url, args.user or "", pw)
        if not ok and not args.force:
            raise ValueError(f"not saved: {why} (re-run with --force to save it anyway)")
        db.catalog_put(args.id, args.name.strip(), args.url.strip(), args.user or "", pw, True)
        db.audit("catalog_add", None, "tui", f"{args.id} {args.url[:120]}")
        return {"ok": True, "detail": why}
    if args.what == "remove":
        if not db.catalog_delete(args.id):
            raise ValueError(f"no catalog '{args.id}'")
        db.audit("catalog_remove", None, "tui", args.id)
        return {"ok": True}
    row = next((r for r in db.catalog_rows() if r["id"] == args.id), None)
    if not row:
        raise ValueError(f"no catalog '{args.id}'")
    db.catalog_put(row["id"], row["name"], row["url"], row["user"], "", args.what == "enable")
    db.audit(f"catalog_{args.what}", None, "tui", args.id)
    return {"ok": True}

def _wanted(args):
    """Keep-looking entries for the TUI: every reader's, open and recently closed."""
    if args.what == "list":
        rows = db.wanted_list(None)
        return {"ok": True, "rows": [{k: w.get(k) for k in ("id", "owner", "kind", "title", "author", "status",
                                                           "checks", "next_check", "detail", "rid", "work_key")}
                                     for w in rows]}
    if not db.wanted_update(args.wid, only_if_open=True, status="cancelled", detail="cancelled by the admin (TUI)"):
        raise ValueError(f"entry {args.wid} is not open")
    db.audit("wanted_cancel", None, "tui", str(args.wid))
    return {"ok": True}

def _canary(args):
    """L08: the synthetic journey (scripts/synthetic.py, on the host) records its runs here, and —
    only while the Turnstile bot check guards the login form, which no script can pass — asks for
    a session for one of the canary accounts. Never for any other account."""
    if args.what == "record":
        run = json.loads(sys.stdin.read() or "{}")
        out = db.canary_record(run)
        if not out["ok"]:
            db.audit("canary_failed", None, "host", f"{out['failed'] or 'journey'}"[:200])
        return {"ok": True, "id": out["id"], "passed": out["ok"], "failed": out["failed"]}
    if args.what == "recent":
        return {"ok": True, "rows": db.canary_recent(args.limit)}
    import secrets, auth
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    if args.name not in config.CANARY_USERS:
        raise ValueError(f"'{args.name}' is not a canary account (CANARY_USERS)")
    fp = auth.fingerprint(args.name)
    if not fp or fp is auth.UNAVAILABLE:
        raise ValueError(f"canary account '{args.name}' is missing from Calibre-Web")
    shell = Flask("canary"); shell.secret_key = config.SECRET_KEY    # the portal's own key and defaults
    tok = secrets.token_urlsafe(32)
    cookie = SecureCookieSessionInterface().get_signing_serializer(shell).dumps(
        {"_permanent": True, "user": args.name, "admin": False, "fp": fp[0], "chk": time.time(), "csrf": tok})
    db.audit("canary_session", args.name, "host", "minted (the login form has the Turnstile check)")
    return {"ok": True, "cookie": cookie, "csrf": tok}

def _gate(args):
    """L05: the host applies portal password changes to Authelia's user file (scripts/gate-sync.py)."""
    if args.what == "pending":
        return {"ok": True, "rows": db.gate_pending()}
    st = db.gate_done(args.user, args.outcome, args.reason)
    db.audit("gate_sync", args.user, "host", f"{args.outcome} {args.reason}"[:200])
    return {"ok": True, "status": st}

def _parser():
    p = argparse.ArgumentParser(prog="admin_cli", description="Admin actions for bookstack.sh")
    sp = p.add_subparsers(dest="cmd", required=True)

    lo = sp.add_parser("lockout").add_subparsers(dest="what", required=True)
    lo.add_parser("status")
    c = lo.add_parser("clear")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--user"); g.add_argument("--ip"); g.add_argument("--all", action="store_true")

    rq = sp.add_parser("requests").add_subparsers(dest="what", required=True)
    ls = rq.add_parser("list")
    ls.add_argument("--status"); ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--offset", type=int, default=0)
    rq.add_parser("retry").add_argument("rid", type=int)
    rq.add_parser("dismiss").add_argument("rid", type=int)

    pk = sp.add_parser("parked").add_subparsers(dest="what", required=True)
    pk.add_parser("list")
    pk.add_parser("retry").add_argument("token")
    pk.add_parser("delete").add_argument("token")

    pu = sp.add_parser("pushes").add_subparsers(dest="what", required=True)
    pu.add_parser("pending").add_argument("--limit", type=int, default=50)
    rs = pu.add_parser("result")
    rs.add_argument("push_id", type=int)
    rs.add_argument("outcome", choices=("ok", "fail"))
    rs.add_argument("--reason", default="")

    tg = sp.add_parser("tags").add_subparsers(dest="what", required=True)
    tg.add_parser("pending").add_argument("--limit", type=int, default=50)
    tr = tg.add_parser("result")
    tr.add_argument("push_id", type=int)
    tr.add_argument("outcome", choices=("ok", "fail"))
    tr.add_argument("--reason", default="")

    cv = sp.add_parser("converts").add_subparsers(dest="what", required=True)
    cv.add_parser("pending").add_argument("--limit", type=int, default=3)
    cr = cv.add_parser("result")
    cr.add_argument("job_id", type=int)
    cr.add_argument("outcome", choices=("ok", "fail"))
    cr.add_argument("--reason", default="")

    ca = sp.add_parser("catalogs").add_subparsers(dest="what", required=True)
    ca.add_parser("list")
    for name in ("add", "test"):
        a = ca.add_parser(name)
        if name == "add":
            a.add_argument("id"); a.add_argument("name")
            a.add_argument("--force", action="store_true")
        a.add_argument("url")
        a.add_argument("--user", default="")
        a.add_argument("--password-stdin", action="store_true")
    for name in ("remove", "enable", "disable"):
        ca.add_parser(name).add_argument("id")

    wa = sp.add_parser("wanted").add_subparsers(dest="what", required=True)
    wa.add_parser("list")
    wa.add_parser("cancel").add_argument("wid", type=int)

    cn = sp.add_parser("canary").add_subparsers(dest="what", required=True)
    cn.add_parser("record")
    cn.add_parser("recent").add_argument("--limit", type=int, default=10)
    cn.add_parser("session").add_argument("name")

    gt = sp.add_parser("gate").add_subparsers(dest="what", required=True)
    gt.add_parser("pending")
    gd = gt.add_parser("done")
    gd.add_argument("user"); gd.add_argument("outcome", choices=("ok", "missing", "failed"))
    gd.add_argument("--reason", default="")
    return p

ARMS = {"lockout": _lockout, "requests": _requests, "parked": _parked, "pushes": _pushes,
        "catalogs": _catalogs, "wanted": _wanted, "tags": _tags, "converts": _converts,
        "canary": _canary, "gate": _gate}

def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        db.init()
        print(json.dumps(ARMS[args.cmd](args)))
        return 0
    except Exception as e:
        # one line of JSON on stdout whatever went wrong: the TUI parses stdout, and a
        # traceback in a whiptail box tells the admin nothing
        msg = str(e) if isinstance(e, (ValueError, OSError)) else f"{e.__class__.__name__}: {e}"
        print(json.dumps({"ok": False, "error": msg[:300]}))
        return 1

if __name__ == "__main__":
    sys.exit(main())
