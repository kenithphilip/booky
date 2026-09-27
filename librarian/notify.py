"""Notifications. Two channels, both best-effort and never blocking a request:
  - NOTIFY_WEBHOOK: a POST on every event. Generic hooks get JSON; ntfy (the free default the
    installer suggests) gets a plain-text body with Title/Priority headers so the phone shows
    a readable message (NOTIFY_WEBHOOK_FORMAT=auto|json|ntfy).
  - e-mail (the same SMTP as Send-to-Kindle): the requester gets a note when their request is
    done, denied or failed (if they opted in on Devices; own uploads only on failure); the
    admin gets a note when a request waits for approval or imported without an owner tag
    (if ADMIN_EMAIL is set)."""
import json, urllib.request, threading, sys
from urllib.parse import urlsplit
from email.message import EmailMessage
import config, db

USER_EVENTS = {"done": "is in your library", "denied": "was denied", "error": "could not be added",
               "needs-tag": "was imported but the admin has to tag it to you first",
               # keep looking (wanted.py): the entry's own events, told to the reader who asked
               "wanted-found": "turned up in a catalog and has been requested for you",
               "wanted-candidate": "may have turned up: confirm it is the right book on your Status page",
               "wanted-expired": "did not turn up in any catalog, so we have stopped looking"}
EVENTS = set(USER_EVENTS) | {"requested", "approved"}

def send(event, req):
    # the canary journey's hidden accounts (L08) report through their own run, never as a
    # "library: done" on the family's phones twice a day
    if req.get("owner") in config.CANARY_USERS:
        return
    _webhook(event, req)
    threading.Thread(target=_mail, args=(event, req), daemon=True).start()

def _ntfy(url):
    fmt = config.NOTIFY_WEBHOOK_FORMAT
    return fmt == "ntfy" or (fmt == "auto" and "ntfy" in (urlsplit(url).hostname or ""))

def _post(url, title, text, priority, payload):
    """One webhook POST in the configured shape. Raises on failure."""
    if _ntfy(url):
        prio = {"high": "high", "urgent": "urgent", "low": "low"}.get(priority, "default")
        # HTTP headers must be latin-1: keep the title ASCII-safe, the body carries the details
        hdr = {"Title": title.encode("ascii", "replace").decode()[:200], "Priority": prio,
               "Content-Type": "text/plain; charset=utf-8"}
        r = urllib.request.Request(url, data=text.encode("utf-8"), headers=hdr)
    else:
        r = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=8)

def _webhook(event, req):
    url = config.NOTIFY_WEBHOOK
    if not url:
        return
    body = {"event": event, "title": req.get("title"), "author": req.get("author"), "owner": req.get("owner"),
            "source": req.get("source"), "status": req.get("status"), "detail": req.get("detail")}
    text = f"{req.get('owner')}: \"{req.get('title')}\" {USER_EVENTS.get(event, event)}" \
        + (f"\n{req.get('detail')}" if req.get("detail") and event != "done" else "")
    try:
        _post(url, f"library: {event}", text, "high" if event == "error" else "default", body)
    except Exception:
        pass

def _mail(event, req):
    import kindle, cwa                     # late import: kindle owns the SMTP session helper
    if not kindle.configured():
        return
    try:
        if event == "requested" and req.get("status") == "pending" and config.ADMIN_EMAIL:
            _deliver(config.ADMIN_EMAIL, f"[library] approval needed: {req.get('title')}",
                     f"{req.get('owner')} requested \"{req.get('title')}\" ({req.get('author') or 'unknown author'}) "
                     f"from {req.get('source')}.\n\nApprove or deny it in the portal: "
                     f"https://request.{config.DOMAIN}/status\n")
        if event == "needs-tag" and config.ADMIN_EMAIL:
            _deliver(config.ADMIN_EMAIL, f"[library] needs an owner tag: {req.get('title')}",
                     f"\"{req.get('title')}\" was imported for {req.get('owner')} but carries no owner tag, so "
                     f"{req.get('owner')} cannot see it.\nDetail: {req.get('detail') or ''}\n\n"
                     f"Add the tag in Calibre-Web or Audiobookshelf, then dismiss it: https://request.{config.DOMAIN}/admin\n")
        # own uploads / dropbox files: only failures and untaggable imports are worth a mail
        own = req.get("source") == "dropbox"
        if event in USER_EVENTS and req.get("owner") and not (own and event in ("done", "denied")):
            if not db.get_prefs(req["owner"])["notify_email"]:
                return
            u = cwa.get_user(req["owner"]) or {}
            if u.get("email") and "@" in u["email"]:
                where = "https://request." + config.DOMAIN + ("/library" if event == "done" else "/status")
                _deliver(u["email"], f"[library] {req.get('title')} {USER_EVENTS[event]}",
                         f"Your request \"{req.get('title')}\" {USER_EVENTS[event]}.\n"
                         + (f"Detail: {req.get('detail')}\n" if req.get("detail") and event != "done" else "")
                         + f"\n{where}\n")
    except Exception:
        pass

def _deliver(to, subject, text):
    import kindle
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    kindle._deliver(msg)

def alert(title, text, priority="default"):
    """Operational alert (backup failed, disk full, container unhealthy, audiobook left
    untagged): webhook + admin mail. Best-effort; returns a list of the channels that took it
    (empty = nobody was told; the reason is printed to stderr)."""
    import kindle
    took = []
    if config.NOTIFY_WEBHOOK:
        try:
            _post(config.NOTIFY_WEBHOOK, f"[bookstack] {title}", text, priority,
                  {"event": "alert", "title": title, "text": text, "priority": priority})
            took.append("webhook")
        except Exception as e:
            print(f"alert: webhook failed: {e}", file=sys.stderr)
    if config.ADMIN_EMAIL and kindle.configured():
        try:
            _deliver(config.ADMIN_EMAIL, f"[bookstack] {title}", f"{text}\n\npriority: {priority}\n"); took.append("mail")
        except Exception as e:
            print(f"alert: mail failed: {e}", file=sys.stderr)
    return took

def why_undelivered():
    """Human reason an alert reached nobody (for the CLI and the admin page)."""
    import kindle
    missing = []
    if not config.NOTIFY_WEBHOOK:
        missing.append("NOTIFY_WEBHOOK is empty")
    if not config.ADMIN_EMAIL:
        missing.append("ADMIN_EMAIL is empty")
    elif not kindle.configured():
        missing.append("outgoing mail (SMTP) is not configured")
    return "; ".join(missing) or "every configured channel failed (see the errors above)"

def _cli(argv):
    """python -m notify alert "<title>" "<text>" [priority]   (scripts/alert.sh, systemd OnFailure=)
    Exit 0 when at least one channel took the alert, 3 when nothing was delivered (so the host
    side logs it to the journal and tries its own webhook), 2 on usage errors."""
    if len(argv) >= 3 and argv[0] == "alert":
        sent = alert(argv[1], argv[2], argv[3] if len(argv) > 3 else "default")
        if sent:
            print(json.dumps({"ok": True, "sent": sent}))
            return 0
        reason = why_undelivered()
        print(json.dumps({"ok": False, "sent": [], "reason": reason}))
        print(f"alert NOT delivered: {reason}", file=sys.stderr)
        return 3
    print('usage: python -m notify alert "<title>" "<text>" [priority]', file=sys.stderr)
    return 2

if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
