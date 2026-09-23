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
import argparse, base64, binascii, json, os, shutil, sys
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
    return p

ARMS = {"lockout": _lockout, "requests": _requests, "parked": _parked}

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
