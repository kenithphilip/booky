#!/usr/bin/env bash
# cert-watch.sh — certificate and token expiry watch (L09). Cron, daily.
#   * every certificate Caddy holds (caddy/data/caddy/certificates/**.crt): Caddy renews at
#     ~30 days left, so under 14 days means renewal has been FAILING for two weeks
#   * caddy/cf-origin-pull-ca.pem (what Caddy trusts for origin pulls): under 60 days, renew it
#   * /etc/bookstack/aop/client.pem (the zone's own origin-pull certificate, L14): under 60 days
#   * the Cloudflare API token (CF_API_TOKEN): /user/tokens/verify must say active, and a token
#     with an expiry date is flagged 14 days ahead — DNS-01 renewals and fail2ban bans stop with it
# One alert per problem per day (cron frequency); prints a summary line for the journal.
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
ALERT="${CERT_ALERT:-$STACK_DIR/scripts/alert.sh}"
CERT_DIR="${CERT_DIR:-$STACK_DIR/caddy/data/caddy/certificates}"
CA="${CERT_CA:-$STACK_DIR/caddy/cf-origin-pull-ca.pem}"
CF_API="${CF_API:-https://api.cloudflare.com/client/v4}"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
problems=() checked=0 soft=()
STATE="${CERT_STATE:-/etc/bookstack/certwatch.state}"
RETRY_SLEEP="${CERT_RETRY_SLEEP:-20}"
days_left(){ local end; end=$(openssl x509 -enddate -noout -in "$1" 2>/dev/null | cut -d= -f2) || return 1
  [ -n "$end" ] || return 1
  local e; e=$(date -d "$end" +%s 2>/dev/null || date -j -f "%b %e %T %Y %Z" "$end" +%s 2>/dev/null) || return 1
  echo $(( (e - $(date +%s)) / 86400 )); }

if [ -d "$CERT_DIR" ]; then
  while IFS= read -r crt; do
    checked=$((checked+1))
    d=$(days_left "$crt") || { problems+=("cannot read certificate $(basename "$crt")"); continue; }
    name=$(basename "$crt" .crt)
    [ "$d" -lt 14 ] && problems+=("certificate for $name expires in $d days: Caddy's renewal is failing (Operations -> Logs -> caddy; look for 'could not get certificate')")
  done < <(find "$CERT_DIR" -name '*.crt' -type f 2>/dev/null)
fi
if [ -f "$CA" ]; then
  checked=$((checked+1))
  d=$(days_left "$CA") && [ "$d" -lt 60 ] && problems+=("Cloudflare's origin-pull CA expires in $d days: re-run Install -> Cloudflare to fetch the current one")
fi
# L14: this zone's own origin-pull client certificate. Cloudflare presents it to Caddy; when it
# expires every public site answers 526. Security -> Origin lock issues a new one.
AOP="${CERT_AOP:-/etc/bookstack/aop/client.pem}"
if [ "$(envget AOP_MODE)" = zone ] || [ "$(envget AOP_MODE)" = both ]; then
  checked=$((checked+1))
  if [ ! -f "$AOP" ]; then problems+=("AOP_MODE is $(envget AOP_MODE) but $AOP is missing: re-run Security -> Origin lock")
  else d=$(days_left "$AOP") && [ "$d" -lt 60 ] && problems+=("this zone's origin-pull certificate expires in $d days: Security -> Origin lock issues a new one (every public site fails when it lapses)"); fi
fi
tok=$(envget CF_API_TOKEN)
if [ -n "$tok" ]; then
  checked=$((checked+1))
  # three tries a little apart: one dropped request is not a finding (live, 2026-09-29 06:40)
  ans=""; for t in 1 2 3; do
    ans=$(curl -fsS -m 20 -H "Authorization: Bearer $tok" "$CF_API/user/tokens/verify" 2>/dev/null) && break
    ans=""; [ "$t" -lt 3 ] && sleep "$RETRY_SLEEP"
  done
  st=$(printf '%s' "$ans" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("result") or {}; print(r.get("status",""), r.get("expires_on") or "")' 2>/dev/null)
  prev_quiet=$(cat "$STATE" 2>/dev/null | sed -n 's/^cf_no_answer=//p' | head -1)
  if [ -z "$ans" ]; then
    # NO ANSWER is the network, not the token (a refused token answers, with a status): say
    # so only when it happens on two days running, and not as an emergency
    mkdir -p "$(dirname "$STATE")" 2>/dev/null; printf 'cf_no_answer=%s\n' "$(date +%F)" > "$STATE" 2>/dev/null || true
    if [ -n "$prev_quiet" ] && [ "$prev_quiet" != "$(date +%F)" ]; then
      soft+=("api.cloudflare.com has not answered this server's token check on two days running (since $prev_quiet). The token itself may be fine; check the server's outbound network (curl https://api.cloudflare.com).")
    else echo "cert-watch: no answer from api.cloudflare.com (3 tries); checking again tomorrow before alerting"; fi
  elif rm -f "$STATE" 2>/dev/null; [ "${st%% *}" != active ]; then problems+=("the Cloudflare API token is '${st%% *}', not active: DNS-01 renewals and Cloudflare bans stop (Install -> Cloudflare)")
  else
    exp=${st#* }
    if [ -n "$exp" ]; then
      e=$(date -d "$exp" +%s 2>/dev/null || echo 0)
      [ "$e" -gt 0 ] && [ $(( (e - $(date +%s)) / 86400 )) -lt 14 ] && problems+=("the Cloudflare API token expires on $exp: roll it (Install -> Cloudflare)")
    fi
  fi
fi

if [ "${#soft[@]}" -gt 0 ]; then
  ALERT_SEQ=certs-network "$ALERT" "Bookstack: Cloudflare's API is not reachable from the server" "$(printf -- '- %s\n' "${soft[@]}")" >/dev/null 2>&1 || true
fi
if [ "${#problems[@]}" -gt 0 ]; then
  msg=$(printf -- '- %s\n' "${problems[@]}")
  ALERT_SEQ=certs "$ALERT" "Bookstack: certificate/token expiry" "$msg" high >/dev/null 2>&1 || true
  echo "cert-watch: ${#problems[@]} problem(s) in $checked item(s)"; printf '%s\n' "$msg"
  exit 1
fi
echo "cert-watch: $checked item(s) fine"
exit 0
