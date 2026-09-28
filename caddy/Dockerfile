# Caddy with: Cloudflare DNS (wildcard/DNS-01 certs, works with origin locked down),
# Cloudflare IP list (real client IPs behind the proxy), and a rate limiter.
# Every module is pinned (looked up 2026-09-22) so an Update rebuild (`build --pull`) cannot
# silently pull new plugin code into the process that holds the Cloudflare DNS token.
# caddy-cloudflare-ip has no release tags: it is pinned to its latest commit as a Go
# pseudo-version (proxy.golang.org @latest, commit f53b62aa13cb).
#
# CADDY IS v2.10.2 (v5.6, 2026-09-28), not the builder's own 2.11.4: GHSA-6365-7ppr-5r92 (moderate)
# says 2.11.x can send a request on the wrong upstream connection when forward_auth and
# reverse_proxy share a handler — exactly our Authelia gate (authelia/inject-gate.py) — which
# shows up as random 404s and, if the wrong upstream answers 200, as a skipped login check. The
# fix is 2.11.5, unreleased as of that date; the advisory's own workaround is 2.10.2. `xcaddy
# build v2.10.2` compiles that release's source with THIS builder's newer Go toolchain, so the Go
# standard library fixes since February 2026 are kept. The runtime base is only Alpine and CA
# certificates. When 2.11.5 ships: drop the version argument and bump both tags to it.
# `caddy version` in the built image must say v2.10.2 (tests/caddy-build-test.sh): a plugin that
# required a newer Caddy would silently lift it back to 2.11.x.
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build v2.10.2 \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/mholt/caddy-ratelimit@v0.1.0 \
    --with github.com/WeidiDeng/caddy-cloudflare-ip@v0.0.0-20231130002422-f53b62aa13cb

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
