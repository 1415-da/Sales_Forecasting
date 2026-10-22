#!/usr/bin/env bash
# Point the Vercel production deployment at the API's current tunnel hostname.
#
# The api-tunnel service is a Cloudflare quick tunnel, whose hostname changes
# each time the container is recreated. Next.js resolves its rewrites at build
# time, so a new hostname needs a new production build: this script updates
# API_PROXY_TARGET and redeploys.
#
# Usage: scripts/publish-tunnel.sh        (run from anywhere; needs `vercel login`)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${TUNNEL_CONTAINER:-$(docker ps --filter 'name=api-tunnel' --format '{{.Names}}' | head -1)}"

if [ -z "$CONTAINER" ]; then
  echo "No running api-tunnel container. Start the stack first." >&2
  exit 1
fi

URL="$(docker logs "$CONTAINER" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)"
if [ -z "$URL" ]; then
  echo "Tunnel hostname not found in $CONTAINER logs yet; retry in a few seconds." >&2
  exit 1
fi

echo "Tunnel: $URL"
# Resolved over DNS-over-HTTPS so a lookup made before the hostname exists is
# not cached by macOS as "not found" (see tunnel-watchdog.sh).
curl -fsS -m 20 --doh-url https://1.1.1.1/dns-query "$URL/health" >/dev/null || echo "warning: $URL/health is not answering yet" >&2

# The Vercel CLI finds the project from frontend/.vercel, or from
# VERCEL_ORG_ID/VERCEL_PROJECT_ID when run outside the repo (the watchdog).
[ -n "${VERCEL_PROJECT_ID:-}" ] || cd "$ROOT/frontend"
vercel env rm API_PROXY_TARGET production --yes >/dev/null 2>&1 || true
printf '%s' "$URL" | vercel env add API_PROXY_TARGET production >/dev/null
# Rebuild the live production deployment from its Git commit with the new
# value. The project's Root Directory is `frontend`, so a `vercel deploy` from
# here would look for frontend/frontend.
vercel redeploy "${VERCEL_PRODUCTION_URL:-https://sales-forecasting-mu.vercel.app}" --environment production
