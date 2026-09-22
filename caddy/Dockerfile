# Caddy with: Cloudflare DNS (wildcard/DNS-01 certs, works with origin locked down),
# Cloudflare IP list (real client IPs behind the proxy), and a rate limiter.
# Every module is pinned (looked up 2026-09-22) so an Update rebuild (`build --pull`) cannot
# silently pull new plugin code into the process that holds the Cloudflare DNS token.
# Bump the two caddy tags together. caddy-cloudflare-ip has no release tags: it is pinned to
# its latest commit as a Go pseudo-version (proxy.golang.org @latest, commit f53b62aa13cb).
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/mholt/caddy-ratelimit@v0.1.0 \
    --with github.com/WeidiDeng/caddy-cloudflare-ip@v0.0.0-20231130002422-f53b62aa13cb

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
