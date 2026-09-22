"""Notifications. Two channels, both best-effort and never blocking a request:
  - NOTIFY_WEBHOOK: JSON POST on every event (Apprise / ntfy / chat relay / your own hook)
  - e-mail (the same SMTP as Send-to-Kindle): the requester gets a note when their request is
    done, denied or failed (if they opted in on Devices); the admin gets a note when a request
    is waiting for approval (if ADMIN_EMAIL is set)."""
import json, urllib.request, threading, sys
from email.message import EmailMessage
import config, db

USER_EVENTS = {"done": "is in your library", "denied": "was denied", "error": "could not be fetched"}

def send(event, req):
    _webhook(event, req)
    threading.Thread(target=_mail, args=(event, req), daemon=True).start()

def _webhook(event, req):
    url = config.NOTIFY_WEBHOOK
    if not url:
        return
    body = {"event": event, "title": req.get("title"), "author": req.get("author"), "owner": req.get("owner"),
            "source": req.get("source"), "status": req.get("status"), "detail": req.get("detail")}
    try:
        data = json.dumps(body).encode()
        r = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=8)
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
        if event in USER_EVENTS and req.get("owner") and req.get("source") != "dropbox":
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
    """Operational alert (backup failed, disk full, container unhealthy): webhook + admin mail.
    Best-effort; returns a list of the channels that took it."""
    import kindle
    took = []
    if config.NOTIFY_WEBHOOK:
        try:
            data = json.dumps({"event": "alert", "title": title, "text": text, "priority": priority}).encode()
            r = urllib.request.Request(config.NOTIFY_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(r, timeout=8); took.append("webhook")
        except Exception as e:
            print(f"alert: webhook failed: {e}", file=sys.stderr)
    if config.ADMIN_EMAIL and kindle.configured():
        try:
            _deliver(config.ADMIN_EMAIL, f"[bookstack] {title}", f"{text}\n\npriority: {priority}\n"); took.append("mail")
        except Exception as e:
            print(f"alert: mail failed: {e}", file=sys.stderr)
    return took

if __name__ == "__main__":
    # python -m notify alert "<title>" "<text>" [priority]   (scripts/alert.sh, systemd OnFailure=)
    if len(sys.argv) >= 4 and sys.argv[1] == "alert":
        print(json.dumps({"ok": True, "sent": alert(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "default")}))
    else:
        print('usage: python -m notify alert "<title>" "<text>" [priority]', file=sys.stderr)
    sys.exit(0)        # an alert helper must never fail the unit that called it
