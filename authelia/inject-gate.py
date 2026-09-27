#!/usr/bin/env python3
"""Inject the Authelia forward_auth gate into a rendered Caddyfile, one bypass list per host.

    python3 inject-gate.py <Caddyfile> <caddy-gate.snippet>

Replaces each marker line `# @AUTHELIA_GATE:<host>@` (host = books, audio, request, shelf)
with the snippet, its `@@BYPASS@@` filled with that host's device/API paths. A host with an
empty list gets forward_auth WITHOUT a matcher (everything gated). The Caddyfile is read
fully before it is opened for writing (v3 truncated it). Exit 1 with a message when there is
no marker at all, an unknown host, or the snippet lacks the placeholder.

Keep BYPASS in step with authelia/configuration.yml.template (same paths as regex rules).
"""
import re
import sys

MARKER = re.compile(r"^[ \t]*# @AUTHELIA_GATE:([a-z]+)@[ \t]*$")
BYPASS = {
    # Kobo sync (token), OPDS and KOReader sync (HTTP Basic) - devices cannot do SSO
    "books": "/kobo/* /opds /opds/* /kosync /kosync/*",
    # Audiobookshelf apps: their own token login, first-run root creation (POST /init — with the
    # gate on there is no Authelia account yet either, so gating it traps the admin), token
    # refresh (POST /auth/refresh; the access token lives 1 h, so without it the apps log out or
    # stop syncing hourly), OIDC if it is ever turned on, API, sockets, streams and public feeds.
    # "/socket.io /socket.io/*" (not just the prefix) so a client that asks for the bare
    # /socket.io?EIO=4 is bypassed here too — configuration.yml.template already allows it.
    # No "/s/*": ghcr.io/advplyr/audiobookshelf:2.36.1 has no route under /s (verified
    # against Server.js and the client bundle). The share path is the client route
    # /share/:slug, whose data comes from /public/* — see configuration.yml.template.
    "audio": "/login /logout /init /auth/refresh /auth/openid /auth/openid/* /api/* /socket.io /socket.io/* /hls/* /ping /status /healthcheck /public/* /feed/*",
    # intake webhook (bearer INTAKE_TOKEN); /healthz stays gated - health checks use loopback
    "request": "/intake",
    # Shelfmark: nothing bypassed
    "shelf": "",
}


def bypass_regexp(paths):
    """Caddy's `path` matcher is case-insensitive and not anchored the way the Authelia rules
    are, so emit one anchored, case-sensitive regexp instead: `/x/*` -> `^/x/`, `/x` -> `^/x$`
    (`/x` and `/x/*` together -> `^/x(/|$)`)."""
    exact, prefix = [], []
    for p in paths.split():
        if p.endswith("/*"):
            prefix.append(p[:-2])
        else:
            exact.append(p)
    alts = []
    for p in sorted(set(prefix) | set(exact)):
        if p in prefix and p in exact:
            alts.append(re.escape(p) + "(/|$)")
        elif p in prefix:
            alts.append(re.escape(p) + "/")
        else:
            alts.append(re.escape(p) + "$")
    return "^(?:" + "|".join(alts) + ")"


def render(snippet, host):
    paths = BYPASS[host]
    if paths:
        return snippet.replace("not path @@BYPASS@@", "not path_regexp " + bypass_regexp(paths)).replace("@@BYPASS@@", bypass_regexp(paths))
    lines = [l for l in snippet.split("\n") if "@@BYPASS@@" not in l]
    return ("\n".join(lines).replace("forward_auth @authelia_protected ", "forward_auth ")
            .replace("request_header @authelia_protected ", "request_header "))


def inject(src, snippet):
    if "@@BYPASS@@" not in snippet:
        raise SystemExit("snippet has no @@BYPASS@@ placeholder")
    out, seen = [], []
    for line in src.split("\n"):
        m = MARKER.match(line)
        if not m:
            out.append(line)
            continue
        host = m.group(1)
        if host not in BYPASS:
            raise SystemExit(f"unknown gate marker @AUTHELIA_GATE:{host}@ (known: {' '.join(BYPASS)})")
        seen.append(host)
        out.append(render(snippet, host))
    if not seen:
        raise SystemExit("no gate markers in Caddyfile; re-render first")
    return "\n".join(out), seen


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__.strip().split("\n")[2].strip())
    cf, snip = argv[1], argv[2]
    with open(snip) as f:
        snippet = f.read().rstrip("\n")
    with open(cf) as f:          # read everything first ...
        src = f.read()
    text, seen = inject(src, snippet)
    with open(cf, "w") as f:     # ... only then truncate and write
        f.write(text)
    print(f"gate injected: {' '.join(seen)}")


if __name__ == "__main__":
    main(sys.argv)
