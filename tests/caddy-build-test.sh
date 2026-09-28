#!/usr/bin/env bash
# caddy-build-test.sh — builds caddy/Dockerfile exactly as Deploy does and proves, on that image:
#   * `caddy version` is the release caddy/Dockerfile asks for (CADDY_EXPECT, default v2.10.2): a
#     plugin whose go.mod needs a newer Caddy would silently lift it back to 2.11.x and undo the
#     GHSA-6365-7ppr-5r92 workaround
#   * the three plugins are compiled in (Cloudflare DNS, Cloudflare IP ranges, rate limiter)
#   * the production Caddyfile, rendered from caddy/Caddyfile.template with EVERY optional site
#     (torrents, Ephemera, Authelia) and the Authelia gate injected, validates on it
# Needs Docker. Usage: bash tests/caddy-build-test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CADDY_EXPECT="${CADDY_EXPECT:-v2.10.2}"
IMG="bookstack/caddy:buildtest"
T="$(mktemp -d "${TMPDIR:-/tmp}/caddybuild.XXXXXX")"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  [PASS] $1"; pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

echo "== building caddy/Dockerfile"
if docker build -q -t "$IMG" "$REPO/caddy" > "$T/build.log" 2>&1; then ok "caddy/Dockerfile builds"
else bad "caddy/Dockerfile does not build"; tail -20 "$T/build.log"; echo "passed $pass, failed $fail"; exit 1; fi

v=$(docker run --rm "$IMG" caddy version 2>/dev/null | awk '{print $1}')
[ "$v" = "$CADDY_EXPECT" ] && ok "caddy version is $v" || bad "caddy version is '$v', expected $CADDY_EXPECT"
mods=$(docker run --rm "$IMG" caddy list-modules 2>/dev/null)
for m in dns.providers.cloudflare http.ip_sources.cloudflare http.handlers.rate_limit; do
  printf '%s\n' "$mods" | grep -qx "$m" && ok "module $m compiled in" || bad "module $m missing"
done

echo "== the production Caddyfile validates on it"
# every optional vhost kept (nothing dropped), placeholders filled the way render_caddyfile does
hash=$(printf 'buildtest-pass\n' | docker run --rm -i "$IMG" caddy hash-password 2>/dev/null)
python3 - "$REPO/caddy/Caddyfile.template" "$T/Caddyfile" "$hash" <<'PY' || bad "could not render the template"
import sys
s = open(sys.argv[1]).read()
for k, v in {"DOMAIN": "example.test", "ADMIN_EMAIL": "admin@example.test", "BIND_IP": "127.0.0.1",
             "TAILSCALE_IP": "100.100.100.100", "ADMIN_HASH": sys.argv[3], "KOBO_RS_BLOCK": ""}.items():
    s = s.replace("@@%s@@" % k, v)
assert "@@" not in s, "an unknown placeholder is left"
open(sys.argv[2], "w").write(s)
PY
python3 "$REPO/authelia/inject-gate.py" "$T/Caddyfile" "$REPO/authelia/caddy-gate.snippet" >/dev/null 2>&1 \
  && ok "the Authelia gate injects" || bad "inject-gate.py failed"
grep -q forward_auth "$T/Caddyfile" && ok "the rendered file carries forward_auth" || bad "no forward_auth in the rendered file"
# the origin lock's trust pool: any CA certificate will do for validation
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=buildtest -keyout "$T/ca.key" -out "$T/ca.pem" >/dev/null 2>&1
if out=$(docker run --rm -e CF_API_TOKEN=0123456789abcdefghijABCDEFGHIJ0123456789 -v "$T/Caddyfile:/etc/caddy/Caddyfile:ro" \
          -v "$T/ca.pem:/etc/caddy/cf-origin-pull-ca.pem:ro" "$IMG" \
          caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1); then
  ok "caddy validate accepts the full production Caddyfile"
else
  bad "caddy validate rejects it:"; printf '%s\n' "$out" | tail -8 | sed 's/^/       /'
fi
docker rmi "$IMG" >/dev/null 2>&1 || true
echo "passed $pass, failed $fail"
[ "$fail" = 0 ]
