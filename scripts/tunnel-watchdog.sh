#!/usr/bin/env bash
# Keep the Vercel site connected to the local API.
#
# A Cloudflare quick tunnel does not survive a long network outage (the Mac
# sleeping, Wi-Fi dropping): Cloudflare releases the hostname and cloudflared
# keeps retrying a tunnel that no longer exists. The site then loads but every
# /auth and /v1 call returns 502, so nobody can sign in.
#
# Run periodically (see scripts/install-tunnel-watchdog.sh). Each run:
#   - does nothing if the site is healthy, or if the local API is down;
#   - republishes Vercel if the tunnel works but the site points elsewhere;
#   - otherwise recreates api-tunnel and republishes.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SITE="${VERCEL_PRODUCTION_URL:-https://sales-forecasting-mu.vercel.app}"
LOCAL_API="${LOCAL_API_URL:-http://localhost:8010}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Tunnel hostnames are probed over DNS-over-HTTPS. A brand-new hostname is
# looked up before it exists, and macOS caches that "not found" answer for
# minutes -- through the system resolver a healthy tunnel then looks dead.
tunnel_ok() {
  [ -n "$1" ] && curl -fsS -m 20 --doh-url https://1.1.1.1/dns-query "$1/health" >/dev/null 2>&1
}

if curl -fsS -m 30 "$SITE/health" >/dev/null 2>&1; then
  log "ok"
  exit 0
fi

curl -fsS -m 10 "$LOCAL_API/health" >/dev/null 2>&1 || { log "local API down; skipping"; exit 0; }

CONTAINER="$(docker ps -a --filter 'name=api-tunnel' --format '{{.Names}}' | head -1)"
[ -n "$CONTAINER" ] || { log "no api-tunnel container; skipping"; exit 0; }

tunnel_url() {
  docker logs "$CONTAINER" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
}

URL="$(tunnel_url)"
if ! tunnel_ok "$URL"; then
  log "tunnel ${URL:-<none>} is dead; recreating $CONTAINER"
  docker restart "$CONTAINER" >/dev/null
  for _ in $(seq 1 30); do
    sleep 2
    NEW="$(tunnel_url)"
    [ -n "$NEW" ] && [ "$NEW" != "$URL" ] && break
  done
  URL="$NEW"
  for _ in $(seq 1 15); do tunnel_ok "$URL" && break; sleep 4; done
fi

log "site not reaching $URL; republishing"
"$HERE/publish-tunnel.sh"
