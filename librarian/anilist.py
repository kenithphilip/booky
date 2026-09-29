"""AniList: a reader's manga progress, from their Kobo to their AniList list (v5.8).

The admin registers one AniList API client (anilist.co -> Settings -> Developer; redirect URL
https://request.<domain>/anilist/callback) and puts its id and secret in Library -> Comics. Each
reader connects their own account on Devices (AniList's authorization-code grant: the token is
valid for a year and is kept in the portal's database only).

What is sent: when Calibre-Web records a manga volume as FINISHED for a reader (their Kobo's
'Finished', KOReader, the web reader), the series' "volumes read" on their AniList list is set to
the highest finished volume. Never lowered: reading an old volume again, or a volume AniList
already counts, changes nothing. The status becomes Reading, or Completed when the series is
finished on AniList and every volume is read. Only manga, manhwa and manhua: AniList does not
list Western comics. A series is matched to AniList by title (English, romaji, native or a
synonym, exactly); a series that does not match is left out, never guessed."""
import logging, time
import requests
import config, db, cwa, comicrel

log = logging.getLogger("anilist")

AUTHORIZE = "https://anilist.co/api/v2/oauth/authorize"
TOKEN = "https://anilist.co/api/v2/oauth/token"
GRAPHQL = "https://graphql.anilist.co"
TIMEOUT = (5, 20)
MANGA_TAGS = ("Manga", "Manhwa", "Manhua")
PER_RUN = 12                      # AniList allows ~30 requests a minute (degraded); stay far under


class AniListError(Exception):
    pass


class AuthLost(AniListError):
    pass


def configured():
    return bool(config.ANILIST_CLIENT_ID and config.ANILIST_CLIENT_SECRET and config.DOMAIN)


def redirect_uri():
    return f"https://request.{config.DOMAIN}/anilist/callback"


def authorize_url(state):
    from urllib.parse import urlencode
    return f"{AUTHORIZE}?" + urlencode({"client_id": config.ANILIST_CLIENT_ID, "redirect_uri": redirect_uri(),
                                        "response_type": "code", "state": state})


def exchange(code):
    """The code AniList sent back -> an access token."""
    try:
        r = requests.post(TOKEN, timeout=TIMEOUT, headers={"Accept": "application/json"},
                          json={"grant_type": "authorization_code", "client_id": config.ANILIST_CLIENT_ID,
                                "client_secret": config.ANILIST_CLIENT_SECRET, "redirect_uri": redirect_uri(),
                                "code": code})
    except requests.RequestException as e:
        raise AniListError(f"AniList did not answer ({type(e).__name__})") from e
    if r.status_code != 200:
        raise AniListError(f"AniList refused the sign-in (HTTP {r.status_code}): check the client id, secret and redirect URL")
    tok = (r.json() or {}).get("access_token")
    if not tok:
        raise AniListError("AniList sent no token")
    return tok


def _gql(token, query, variables=None):
    try:
        r = requests.post(GRAPHQL, timeout=TIMEOUT, json={"query": query, "variables": variables or {}},
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                   "Content-Type": "application/json"} if token else
                                  {"Accept": "application/json", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise AniListError(f"AniList did not answer ({type(e).__name__})") from e
    if r.status_code in (400, 401) and token and "invalid token" in (r.text or "").lower():
        raise AuthLost("AniList no longer accepts this connection: connect again on Devices")
    if r.status_code == 401:
        raise AuthLost("AniList no longer accepts this connection: connect again on Devices")
    if r.status_code == 429:
        raise AniListError("AniList asked us to slow down")
    d = r.json() if r.content else {}
    errs = [e.get("message", "") for e in (d.get("errors") or [])]
    if r.status_code == 404 or any("not found" in e.lower() for e in errs):
        return None
    if r.status_code != 200 or errs:
        raise AniListError("AniList: " + ("; ".join(errs) or f"HTTP {r.status_code}")[:200])
    return d.get("data") or {}


def viewer(token):
    d = _gql(token, "query { Viewer { id name } }") or {}
    v = d.get("Viewer") or {}
    if not v.get("id"):
        raise AniListError("AniList did not say who this is")
    return v["id"], v.get("name") or ""


def connect(owner, code):
    token = exchange(code)
    uid, name = viewer(token)
    db.anilist_set(owner, token, uid, name)
    return name


# ---- which AniList entry a series is -------------------------------------------------------------------
def match(series_name, token=None):
    """The AniList manga whose English / romaji / native title or a synonym IS this series name
    (normalised), or None. Remembered, a miss too (for a week)."""
    key = comicrel.norm(series_name)
    hit = db.anilist_media_get(key)
    if hit and (hit["media_id"] or time.time() - (hit["at"] or 0) < 7 * 86400):
        return hit if hit["media_id"] else None
    d = _gql(token, """query M($s: String) { Page(perPage: 8) { media(search: $s, type: MANGA) {
                         id volumes status title { romaji english native } synonyms } } }""", {"s": series_name}) or {}
    found = None
    for m in ((d.get("Page") or {}).get("media") or []):
        t = m.get("title") or {}
        names = [t.get("english"), t.get("romaji"), t.get("native")] + list(m.get("synonyms") or [])
        if any(n and comicrel.norm(n) == key for n in names):
            found = m
            break
    if not found:
        db.anilist_media_put(key, None, None, None, None)
        return None
    t = found.get("title") or {}
    db.anilist_media_put(key, found["id"], t.get("english") or t.get("romaji") or series_name,
                         found.get("volumes"), found.get("status"))
    return db.anilist_media_get(key)


# ---- what a reader finished ----------------------------------------------------------------------------
def finished_volumes(owner):
    """{series name: highest finished volume} of the reader's manga, from Calibre-Web's read state."""
    import comics
    state = cwa.reading_state(owner)
    done = [bid for bid, s in state.items() if s["status"] == "read"]
    if not done:
        return {}
    out = {}
    with comics._calibre() as c:
        q = f"""SELECT s.name, b.series_index FROM books b JOIN books_series_link l ON l.book=b.id JOIN series s ON s.id=l.series
                WHERE b.id IN ({','.join('?' * len(done))}) AND EXISTS (SELECT 1 FROM books_tags_link bt JOIN tags t ON t.id=bt.tag
                WHERE bt.book=b.id AND t.name IN ({','.join('?' * len(MANGA_TAGS))}))"""
        for name, idx in c.execute(q, (*done, *MANGA_TAGS)):
            try:
                v = int(float(idx))
            except (TypeError, ValueError):
                continue
            out[name] = max(out.get(name, 0), v)
    return out


def sync_owner(owner, budget):
    """Send what this reader finished. Returns (updated, budget left)."""
    link = db.anilist_get(owner)
    if not link:
        return 0, budget
    updated = 0
    for series, vol in sorted(finished_volumes(owner).items()):
        if budget <= 0:
            break
        m = db.anilist_media_get(comicrel.norm(series))
        if not m:
            budget -= 1
            m = match(series, link["token"])
        if not m or not m.get("media_id"):
            continue
        if vol <= db.anilist_sent(owner, m["media_id"]):
            continue
        budget -= 2
        cur = _gql(link["token"], """query E($u: Int, $m: Int) { MediaList(userId: $u, mediaId: $m) {
                                       status progressVolumes } }""", {"u": link["al_user_id"], "m": m["media_id"]}) or {}
        have = ((cur.get("MediaList") or {}).get("progressVolumes") or 0)
        if vol <= have:
            db.anilist_sent_set(owner, m["media_id"], have)          # AniList already counts it
            continue
        status = "COMPLETED" if (m.get("volumes") and vol >= m["volumes"] and m.get("status") == "FINISHED") else "CURRENT"
        _gql(link["token"], """mutation S($m: Int, $v: Int, $s: MediaListStatus) {
                                 SaveMediaListEntry(mediaId: $m, progressVolumes: $v, status: $s) { id progressVolumes } }""",
             {"m": m["media_id"], "v": vol, "s": status})
        db.anilist_sent_set(owner, m["media_id"], vol)
        updated += 1
    return updated, budget


def sync_once():
    if not configured():
        return 0
    budget, total = PER_RUN, 0
    for link in db.anilist_all():
        if budget <= 0:
            break
        try:
            n, budget = sync_owner(link["owner"], budget)
            total += n
            if n:
                db.anilist_note(link["owner"], f"last update {time.strftime('%Y-%m-%d %H:%M')}: {n} series")
        except AuthLost as e:
            db.anilist_note(link["owner"], str(e))
        except AniListError as e:
            log.warning("AniList sync for %s: %s", link["owner"], e)
            break                                   # most likely the rate limit: next run
    return total
