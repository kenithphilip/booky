# Caddy with: Cloudflare DNS (wildcard/DNS-01 certs, works with origin locked down),
# Cloudflare IP list (real client IPs behind the proxy), and a rate limiter.
# Pinned (looked up 2026-09-22); bump both caddy tags together. caddy-cloudflare-ip has no
# release tags, so it stays unversioned (module-proxy latest).
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/mholt/caddy-ratelimit@v0.1.0 \
    --with github.com/WeidiDeng/caddy-cloudflare-ip

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
