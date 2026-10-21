/**
 * Where the access token lives.
 *
 * In memory, module-scope. Not `localStorage`, and not a readable cookie:
 * either would let any script on the page read the token, which is the whole
 * exposure that pairing a short-lived access token with an HttpOnly refresh
 * cookie is meant to avoid. The cost is that a page reload loses it -- which is
 * exactly why the refresh cookie exists, and the app silently re-establishes
 * the session from it on load.
 *
 * Kept separate from `lib/api.ts` so the client and the React layer can both
 * reach the token without importing each other.
 */

let accessToken: string | null = null;
let expiresAt: number | null = null;

/** Subscribers notified when the session is established or cleared. */
type Listener = (token: string | null) => void;
const listeners = new Set<Listener>();

/**
 * Refresh this many milliseconds before the token actually expires.
 *
 * Without the margin, a request issued at the last moment can arrive after
 * expiry and fail for no reason the user can act on.
 */
export const REFRESH_MARGIN_MS = 60_000;

/** The current access token, or null when there is no session. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** Store a token and its lifetime, notifying subscribers. */
export function setAccessToken(token: string | null, expiresInSeconds?: number): void {
  accessToken = token;
  expiresAt =
    token && expiresInSeconds ? Date.now() + expiresInSeconds * 1000 : null;
  for (const listener of listeners) listener(token);
}

/** Clear the session. */
export function clearAccessToken(): void {
  setAccessToken(null);
}

/**
 * Whether the token is absent or close enough to expiry to be refreshed.
 *
 * Returns false when the lifetime is unknown: guessing it has expired would
 * cause a refresh storm.
 */
export function isExpiring(): boolean {
  if (!accessToken) return true;
  if (expiresAt === null) return false;
  return Date.now() >= expiresAt - REFRESH_MARGIN_MS;
}

/** Subscribe to session changes. Returns an unsubscribe function. */
export function onAccessTokenChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Milliseconds until the token should be refreshed, or null if unknown.
 *
 * Used to schedule a refresh ahead of expiry so a long-idle tab does not
 * discover its session is dead at the moment the user clicks something.
 */
export function millisecondsUntilRefresh(): number | null {
  if (!accessToken || expiresAt === null) return null;
  return Math.max(0, expiresAt - REFRESH_MARGIN_MS - Date.now());
}
