"""Configure Uptime Kuma for this stack, idempotently. Run by bookstack.sh after every Deploy and
from Operations -> Monitoring (docker compose run --rm -T kuma-bootstrap < config.json).

Input: one JSON object on stdin (see bookstack.sh kuma_config). Secrets arrive this way and
never through the environment, so they are not in `docker inspect` or /proc/<pid>/environ.
Output: one JSON line on stdout, {"ok": true, ...} or {"ok": false, "error": ..., "code": ...};
exit 0 / 1 (failed) / 2 (Kuma already has an account and these are not its credentials).

What it owns, and only that:
  * monitors whose description starts with MARK — added, corrected to spec, or deleted when the
    feature they watch is switched off. A monitor the admin adds by hand is never touched.
  * notifications named "bookstack: ..." — the same channels scripts/alert.sh uses (the
    NOTIFY_WEBHOOK and the SMTP account), so Kuma reaches the admin exactly where every other
    alert does, and does so directly: a notification that went through the portal could not
    report the portal being down.
  * one maintenance window around the nightly unattended reboot, so 04:30 is not an alert.
  * three settings: history kept 30 days (Kuma's 180-day default is millions of heartbeat rows
    in a database the nightly backup snapshots), the public base URL, trust of Caddy's
    X-Forwarded-For."""
import json, sys, time
from urllib.parse import urlsplit

MARK = "bookstack-managed:"
NOTE = ("Created by bookstack.sh (Operations -> Monitoring); edits to this monitor are put back "
        "on the next Deploy. Add your own monitors alongside instead.")
NOTIFY_PREFIX = "bookstack: "
MAINT_TITLE = "Nightly unattended reboot (bookstack)"
KEEP_DAYS = 30

# Fields compared when deciding whether a managed monitor needs an edit. Everything the spec sets
# is listed; anything else on the monitor (Kuma's own bookkeeping) is left alone.
COMPARED = ("type", "name", "url", "hostname", "port", "interval", "retryInterval", "maxretries",
            "resendInterval", "maxredirects", "accepted_statuscodes", "jsonPath", "expectedValue",
            "pushToken", "expiryNotification", "timeout", "description")


def _http(key, name, url, **kw):
    spec = {"type": "http", "name": name, "url": url, "interval": 60, "retryInterval": 60,
            "maxretries": 2, "resendInterval": 0, "maxredirects": 10,
            "accepted_statuscodes": ["200-299"], "expiryNotification": False, "timeout": 20}
    spec.update(kw)
    return key, spec


def _json(key, name, url, path, expected, **kw):
    k, spec = _http(key, name, url, **kw)
    spec.update(type="json-query", jsonPath=path, expectedValue=expected)
    return k, spec


def _push(key, name, token, every, resend=0):
    """A dead-man's switch: the job pushes on success; silence for `every` seconds is DOWN.
    maxretries 0: a missed push is already the retry (every job pushes well inside `every`)."""
    return key, {"type": "push", "name": name, "pushToken": token, "interval": every,
                 "retryInterval": every, "maxretries": 0, "resendInterval": resend}


def desired_monitors(cfg):
    """{key: spec} for this configuration. Pure: tested without a Kuma."""
    f = cfg.get("features") or {}
    d = cfg.get("domain") or ""
    out = [
        _http("portal", "Portal (request.) - health", "http://127.0.0.1:8090/healthz"),
        _http("calibre-web", "Calibre-Web (books.)", "http://127.0.0.1:8083/login"),
        # isInit=false is the state in which the first visitor becomes Audiobookshelf's root:
        # the same condition Deploy refuses to start Caddy under, watched continuously
        _json("audiobookshelf", "Audiobookshelf (audio.) - initialised",
              "http://127.0.0.1:13378/status", "isInit", "true"),
        _http("shelfmark", "Shelfmark (shelf.) - health", "http://127.0.0.1:8084/api/health"),
        # Shelfmark silently falls back to auth mode "none" (no login, everyone admin) when it
        # cannot read Calibre-Web's app.db: "up" is not enough, it must still say "cwa"
        _json("shelfmark-auth", "Shelfmark still requires library logins",
              "http://127.0.0.1:8084/api/auth/check", "auth_mode", "cwa"),
    ]
    if cfg.get("bind_ip"):
        # TCP only: the public listener demands Cloudflare's client certificate, so any HTTP
        # request from here is refused at the handshake. A listening socket is the claim.
        out.append(("caddy", {"type": "port", "name": "Caddy - public listener (origin :443)",
                              "hostname": cfg["bind_ip"], "port": 443, "interval": 60,
                              "retryInterval": 60, "maxretries": 2, "resendInterval": 0}))
    if d:
        # The one monitor that leaves the box: DNS -> Cloudflare -> origin pull certificate ->
        # Caddy -> portal, i.e. what a reader's browser does. Every 5 minutes (288 requests a day),
        # not every minute: each one is a full round trip through Cloudflare and back in.
        # With Authelia on, /healthz is behind the gate and answers with the login page (200
        # after the redirect), which still proves the path.
        out.append(_http("public", f"Public path via Cloudflare (request.{d})",
                         f"https://request.{d}/healthz", interval=300, retryInterval=120,
                         maxretries=1, timeout=30))
    if f.get("authelia"):
        out.append(_http("authelia", "Authelia (auth.) - health", "http://127.0.0.1:9091/api/health"))
    if f.get("torrents"):
        out.append(_http("qbittorrent", "qBittorrent (dl.)", "http://127.0.0.1:8080/"))
    if f.get("ephemera"):
        out.append(_http("ephemera", "Ephemera (ephemera.) - health", "http://127.0.0.1:8286/health"))
    if f.get("flaresolverr"):
        out.append(_json("flaresolverr", "FlareSolverr (challenge solver)",
                         "http://127.0.0.1:8191/health", "status", "ok"))
    p = cfg.get("push") or {}
    # every push interval = the job's period plus room for its RandomizedDelaySec and run time
    if p.get("selftest"):
        # resend 24: a self-test that stays red is repeated once a day, not once and then silence
        out.append(_push("push-selftest", "Self-test (hourly)", p["selftest"], 75 * 60, resend=24))
    if p.get("disk"):
        out.append(_push("push-disk", "Disk watchdog (hourly)", p["disk"], 75 * 60))
    if p.get("metapush"):
        out.append(_push("push-metapush", "Metadata push to Calibre (every 15 min)", p["metapush"], 45 * 60))
    if p.get("cfips"):
        out.append(_push("push-cfips", "Cloudflare IP allowlist refresh (nightly)", p["cfips"], 26 * 3600))
    if p.get("backup"):
        out.append(_push("push-backup", "Backup (nightly)", p["backup"], 26 * 3600))
    mons = {}
    for key, spec in out:
        spec["description"] = f"{MARK}{key}. {NOTE}"
        mons[key] = spec
    return mons


def _ntfy_shaped(url, fmt):
    """Same rule as librarian/notify.py _ntfy(), so Kuma's message looks like every other alert."""
    return fmt == "ntfy" or (fmt in ("", "auto") and "ntfy" in (urlsplit(url).hostname or ""))


def desired_notifications(cfg):
    n = cfg.get("notify") or {}
    out = {}
    hook = (n.get("webhook") or "").strip()
    if hook:
        spec = {"type": "webhook", "webhookURL": hook}
        if _ntfy_shaped(hook, (n.get("format") or "auto").strip()):
            # scripts/alert.sh posts ntfy-style (plain body, Title/Priority headers) to this URL;
            # do the same. Kuma's default JSON body would arrive on a phone as a raw JSON blob.
            spec.update(webhookContentType="custom", webhookCustomBody="{{msg}}",
                        # a string body is sent by axios as form-urlencoded unless told otherwise
                        webhookAdditionalHeaders=json.dumps(
                            {"Title": "Bookstack monitor", "Priority": "high", "Tags": "rotating_light",
                             "Content-Type": "text/plain; charset=utf-8"}))
        else:
            spec.update(webhookContentType="json")
        out["webhook"] = spec
    s = n.get("smtp") or {}
    to = (n.get("to") or "").strip()
    if s.get("host") and to and s.get("from"):
        sec = (s.get("security") or "starttls").lower()
        out["e-mail"] = {"type": "smtp", "smtpHost": s["host"], "smtpPort": int(s.get("port") or 587),
                         # nodemailer: secure=true is implicit TLS (465); false upgrades with STARTTLS
                         "smtpSecure": sec == "ssl", "smtpIgnoreTLSError": False,
                         "smtpUsername": s.get("user") or "", "smtpPassword": s.get("password") or "",
                         "smtpFrom": s["from"], "smtpTo": to}
    return {NOTIFY_PREFIX + k: v for k, v in out.items()}


def _norm(v):
    v = getattr(v, "value", v)           # the library hands back enums (MonitorType.HTTP), not "http"
    if isinstance(v, list):
        return sorted(str(x) for x in v)
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    return str(v)


def monitor_changes(current, spec):
    """The spec fields whose value differs on `current` (a monitor as Kuma returns it)."""
    return [k for k in COMPARED if k in spec and _norm(current.get(k)) != _norm(spec[k])]


def plan(existing, desired):
    """(to_add, to_edit, to_delete) from Kuma's monitor list. Only MARKed monitors are ever
    edited or deleted. A duplicate of a managed key (two runs racing, or a monitor copied in the
    UI with its description) is deleted, keeping the lowest id."""
    by_key, dupes = {}, []
    for m in sorted(existing, key=lambda m: m.get("id") or 0):
        desc = m.get("description") or ""
        if not desc.startswith(MARK):
            continue
        key = desc[len(MARK):].split(".", 1)[0].strip()
        if key in by_key:
            dupes.append(m)
        else:
            by_key[key] = m
    to_add = [k for k in desired if k not in by_key]
    to_edit = [(by_key[k], k) for k in desired if k in by_key]
    to_delete = [m for k, m in by_key.items() if k not in desired] + dupes
    return to_add, to_edit, to_delete


def maintenance_spec(reboot):
    """Kuma's cron strategy: the reboot time minus 5 minutes, for 25 minutes, server time zone."""
    try:
        h, m = (int(x) for x in (reboot or "04:30").split(":"))
    except ValueError:
        h, m = 4, 30
    start = (h * 60 + m - 5) % (24 * 60)
    return {"title": MAINT_TITLE, "strategy": "cron", "active": True,
            "description": "The unattended-upgrades reboot restarts every container; the "
                           "post-reboot self-test reports anything that did not come back.",
            "cron": f"{start % 60} {start // 60} * * *", "durationMinutes": 25,
            "timezoneOption": "SAME_AS_SERVER", "intervalDay": 1, "weekdays": [], "daysOfMonth": []}


# ---------------------------------------------------------------------------------------------
def _connect(url, deadline):
    from uptime_kuma_api import UptimeKumaApi
    last = None
    while time.time() < deadline:
        try:
            return UptimeKumaApi(url, timeout=30, wait_events=0.2)
        except Exception as e:           # Kuma still starting (its first boot migrates a DB)
            last = e
            time.sleep(3)
    raise RuntimeError(f"Uptime Kuma did not answer at {url}: {last}")


def apply(cfg):
    from uptime_kuma_api import MonitorType, NotificationType, MaintenanceStrategy
    types = {"http": MonitorType.HTTP, "json-query": MonitorType.JSON_QUERY,
             "port": MonitorType.PORT, "push": MonitorType.PUSH}
    ntypes = {"webhook": NotificationType.WEBHOOK, "smtp": NotificationType.SMTP}
    res = {"ok": True, "setup": "existing", "added": [], "updated": [], "deleted": [],
           "notifications": [], "maintenance": None}
    api = _connect(cfg.get("url") or "http://127.0.0.1:3001", time.time() + int(cfg.get("wait", 90)))
    try:
        user, pw = cfg["user"], cfg["password"]
        if api.need_setup():
            api.setup(user, pw)
            res["setup"] = "created"
        try:
            api.login(user, pw)
        except Exception as e:
            return {"ok": False, "code": "credentials",
                    "error": f"Uptime Kuma refused the login for '{user}' ({e}). It already has an "
                             f"admin account that is not the one in .env (or it has 2FA on): enter "
                             f"that account under Operations -> Monitoring."}

        cur = api.get_settings()
        want = {"keepDataPeriodDays": KEEP_DAYS, "trustProxy": True, "searchEngineIndex": False,
                "checkUpdate": False}
        if cfg.get("domain"):
            want["primaryBaseURL"] = f"https://monitor.{cfg['domain']}"
        if any(cur.get(k) != v for k, v in want.items()):
            merged = {k: v for k, v in cur.items() if k in (
                "checkUpdate", "checkBeta", "keepDataPeriodDays", "serverTimezone", "entryPage",
                "searchEngineIndex", "primaryBaseURL", "steamAPIKey", "nscd", "dnsCache",
                "chromeExecutable", "tlsExpiryNotifyDays", "disableAuth", "trustProxy")}
            merged.update(want)
            api.set_settings(password=pw, **merged)
            res["settings"] = sorted(k for k, v in want.items() if cur.get(k) != v)

        # notifications first: every monitor is attached to all of them
        want_n = desired_notifications(cfg)
        have_n = {n["name"]: n for n in api.get_notifications() if (n.get("name") or "").startswith(NOTIFY_PREFIX)}
        ids = []
        for name, spec in want_n.items():
            data = dict(spec, name=name, type=ntypes[spec["type"]], isDefault=False, applyExisting=False)
            if name in have_n:
                api.edit_notification(have_n[name]["id"], **data)
                ids.append(have_n[name]["id"])
            else:
                ids.append(api.add_notification(**data)["id"])
            res["notifications"].append(name)
        for name, n in have_n.items():
            if name not in want_n:
                api.delete_notification(n["id"])

        want_m = desired_monitors(cfg)
        to_add, to_edit, to_delete = plan(api.get_monitors(), want_m)
        for m in to_delete:
            api.delete_monitor(m["id"])
            res["deleted"].append(m.get("name"))
        managed_ids = []
        for key in to_add:
            spec = dict(want_m[key], type=types[want_m[key]["type"]], notificationIDList=ids)
            # add_monitor() refuses pushToken (the library generates its own); the token is the
            # one bookstack.sh gave the jobs, so it is set by an edit straight after
            token = spec.pop("pushToken", None)
            mid = api.add_monitor(**spec)["monitorID"]
            if token:
                api.edit_monitor(mid, pushToken=token)
            managed_ids.append(mid)
            res["added"].append(want_m[key]["name"])
        for m, key in to_edit:
            spec = want_m[key]
            attached = sorted(int(i) for i in (m.get("notificationIDList") or []))
            why = monitor_changes(m, spec)
            if attached != sorted(ids):
                why.append("notifications")
            if not m.get("active", True):
                why.append("paused")
            if why:
                api.edit_monitor(m["id"], **dict(spec, type=types[spec["type"]], notificationIDList=ids))
                res["updated"].append(f"{spec['name']} ({', '.join(why)})")
                if not m.get("active", True):
                    api.resume_monitor(m["id"])
            managed_ids.append(m["id"])

        ms = maintenance_spec(cfg.get("reboot_time"))
        data = dict(ms, strategy=MaintenanceStrategy.CRON)
        have = [x for x in api.get_maintenances() if x.get("title") == MAINT_TITLE]
        if have:
            mid = have[0]["id"]
            api.edit_maintenance(mid, **data)
            for extra in have[1:]:
                api.delete_maintenance(extra["id"])
        else:
            mid = api.add_maintenance(**data)["maintenanceID"]
        api.add_monitor_maintenance(mid, [{"id": i} for i in managed_ids])
        res["maintenance"] = ms["cron"]
        res["monitors"] = len(managed_ids)
        return res
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def main():
    try:
        cfg = json.load(sys.stdin)
    except ValueError as e:
        print(json.dumps({"ok": False, "code": "input", "error": f"config is not JSON: {e}"}))
        return 1
    if cfg.get("dry_run"):
        print(json.dumps({"ok": True, "monitors": desired_monitors(cfg),
                          "notifications": sorted(desired_notifications(cfg)),
                          "maintenance": maintenance_spec(cfg.get("reboot_time"))}))
        return 0
    try:
        res = apply(cfg)
    except Exception as e:
        res = {"ok": False, "code": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res))
    return 0 if res.get("ok") else (2 if res.get("code") == "credentials" else 1)


if __name__ == "__main__":
    sys.exit(main())
