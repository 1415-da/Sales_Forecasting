'use client';

/**
 * The authentication context.
 *
 * Holds the signed-in user, establishes the session on load, keeps the access
 * token fresh, and exposes `can()` bound to the current user.
 *
 * Session restoration on load is the piece that makes an in-memory access
 * token usable: a page reload loses the token, so the provider immediately
 * tries a refresh against the HttpOnly cookie. Until that resolves the app is
 * in a third state -- neither signed in nor signed out -- which the UI must
 * render as "checking", or it will flash the login page at an
 * already-authenticated user on every reload.
 */

import { useRouter } from 'next/navigation';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import {
  ApiError,
  configureAuth,
  getCurrentUser,
  login as apiLogin,
  logout as apiLogout,
  refreshSession,
  register as apiRegister,
} from '@/lib/api';
import {
  clearAccessToken,
  getAccessToken,
  millisecondsUntilRefresh,
} from '@/lib/auth-store';
import { can as rbacCan, type Permission } from '@/lib/rbac';
import type { UserProfile } from '@/lib/types/auth';

/** Where an unauthenticated visitor is sent. */
export const LOGIN_PATH = '/login';

/** Where a successful sign-in lands. */
export const POST_LOGIN_PATH = '/forecast';

export type AuthStatus = 'checking' | 'authenticated' | 'anonymous';

export interface AuthContextValue {
  readonly status: AuthStatus;
  readonly user: UserProfile | null;
  /** True only once the session is established. */
  readonly isAuthenticated: boolean;
  /** True while the initial session check is in flight. */
  readonly isChecking: boolean;
  readonly signIn: (email: string, password: string) => Promise<UserProfile>;
  readonly signUp: (payload: {
    email: string;
    full_name: string;
    password: string;
  }) => Promise<UserProfile>;
  readonly signOut: (allSessions?: boolean) => Promise<void>;
  /** Re-read `/auth/me`, after a role change or a password change. */
  readonly reload: () => Promise<void>;
  /** Whether the signed-in user holds a capability. */
  readonly can: (permission: Permission) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { readonly children: ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<AuthStatus>('checking');
  const [user, setUser] = useState<UserProfile | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSession = useCallback(() => {
    clearAccessToken();
    setUser(null);
    setStatus('anonymous');
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, []);

  // Let the API client tell us when a refresh failed, so one 401 anywhere in
  // the app ends the session exactly once instead of each query handling it.
  useEffect(() => {
    configureAuth({ onSessionLost: clearSession });
    return () => configureAuth({});
  }, [clearSession]);

  /**
   * Refresh ahead of expiry, rescheduling after each success.
   *
   * Proactive rather than purely reactive: a user who leaves a tab open for an
   * hour should not discover their session died at the moment they click
   * something.
   */
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    const delay = millisecondsUntilRefresh();
    if (delay === null) return;
    refreshTimer.current = setTimeout(async () => {
      const ok = await refreshSession();
      if (ok) {
        scheduleRefresh();
      } else {
        clearSession();
      }
    }, delay);
  }, [clearSession]);

  const loadUser = useCallback(async () => {
    const profile = await getCurrentUser();
    setUser(profile);
    setStatus('authenticated');
    scheduleRefresh();
    return profile;
  }, [scheduleRefresh]);

  // Establish the session on mount. A reload discards the in-memory access
  // token, so the refresh cookie is the only way back to an authenticated
  // state without asking for the password again.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        if (!getAccessToken()) {
          const restored = await refreshSession();
          if (!restored) {
            if (!cancelled) clearSession();
            return;
          }
        }
        await loadUser();
      } catch {
        if (!cancelled) clearSession();
      }
    })();

    return () => {
      cancelled = true;
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, [clearSession, loadUser]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      const result = await apiLogin(email, password);
      setUser(result.user);
      setStatus('authenticated');
      scheduleRefresh();
      return result.user;
    },
    [scheduleRefresh],
  );

  const signUp = useCallback(
    async (payload: { email: string; full_name: string; password: string }) => {
      const result = await apiRegister(payload);
      setUser(result.user);
      setStatus('authenticated');
      scheduleRefresh();
      return result.user;
    },
    [scheduleRefresh],
  );

  const signOut = useCallback(
    async (allSessions = false) => {
      try {
        await apiLogout(allSessions);
      } catch (error) {
        // A failed logout must still sign the user out locally: leaving the
        // client believing it is authenticated is the worse outcome.
        if (!(error instanceof ApiError)) throw error;
      } finally {
        clearSession();
        router.replace(LOGIN_PATH);
      }
    },
    [clearSession, router],
  );

  const reload = useCallback(async () => {
    try {
      await loadUser();
    } catch {
      clearSession();
    }
  }, [clearSession, loadUser]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      isAuthenticated: status === 'authenticated' && user !== null,
      isChecking: status === 'checking',
      signIn,
      signUp,
      signOut,
      reload,
      can: (permission: Permission) => rbacCan(user, permission),
    }),
    [status, user, signIn, signUp, signOut, reload],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The auth context. Throws outside the provider, which is a wiring bug. */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error('useAuth must be used inside <AuthProvider>');
  }
  return context;
}

/**
 * Whether the signed-in user holds a capability.
 *
 * A thin wrapper over `useAuth().can`, so a component that only needs a
 * permission check does not have to destructure the whole context.
 */
export function usePermission(permission: Permission): boolean {
  return useAuth().can(permission);
}
