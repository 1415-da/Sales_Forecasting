'use client';

/**
 * Route protection and permission gating.
 *
 * `RequireAuth` wraps every authenticated page: while the session check is in
 * flight it renders a neutral loading state, and only once the check has
 * *resolved* does it redirect. Redirecting during "checking" would bounce an
 * already-signed-in user to the login page on every reload, because a reload
 * discards the in-memory access token before the refresh completes.
 *
 * `RequirePermission` gates a region of a page. It renders an explicit
 * "you do not have permission" panel rather than hiding the section silently:
 * someone who was told a feature exists needs to learn that their role lacks
 * it, not that the page is broken.
 */

import { Loader2, Lock } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, type ReactNode } from 'react';

import { EmptyState } from '@/components/shared/empty-state';
import { LOGIN_PATH, useAuth } from '@/hooks/use-auth';
import {
  NO_PERMISSION_MESSAGE,
  ROLE_LABELS,
  type Permission,
  type Role,
} from '@/lib/rbac';

/** Full-page neutral placeholder shown while the session is being checked. */
export function SessionChecking() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-h-[60vh] flex-col items-center justify-center gap-3"
    >
      <Loader2
        aria-hidden="true"
        className="h-6 w-6 animate-spin text-muted-foreground"
      />
      <p className="text-sm text-muted-foreground">Checking your session…</p>
    </div>
  );
}

export interface RequireAuthProps {
  readonly children: ReactNode;
  /** Optional capability the page itself requires. */
  readonly permission?: Permission;
}

export function RequireAuth({ children, permission }: RequireAuthProps) {
  const { isAuthenticated, isChecking, user, can } = useAuth();
  const router = useRouter();

  useEffect(() => {
    // Only once the check has resolved. Redirecting while `isChecking` would
    // race the session restore and sign the user out of a valid session.
    if (!isChecking && !isAuthenticated) {
      const next =
        typeof window !== 'undefined'
          ? `?next=${encodeURIComponent(window.location.pathname)}`
          : '';
      router.replace(`${LOGIN_PATH}${next}`);
    }
  }, [isAuthenticated, isChecking, router]);

  if (isChecking || !isAuthenticated) return <SessionChecking />;

  if (permission && !can(permission)) {
    return (
      <PermissionDenied
        role={user?.role as Role | undefined}
        permission={permission}
      />
    );
  }

  return <>{children}</>;
}

export interface RequirePermissionProps {
  readonly permission: Permission;
  readonly children: ReactNode;
  /** Rendered instead of the default panel when the permission is missing. */
  readonly fallback?: ReactNode;
}

export function RequirePermission({
  permission,
  children,
  fallback,
}: RequirePermissionProps) {
  const { can, user } = useAuth();
  if (can(permission)) return <>{children}</>;
  if (fallback !== undefined) return <>{fallback}</>;
  return (
    <PermissionDenied
      role={user?.role as Role | undefined}
      permission={permission}
    />
  );
}

/** The panel shown wherever a capability is missing. */
export function PermissionDenied({
  role,
  permission,
}: {
  readonly role?: Role;
  readonly permission?: Permission;
}) {
  return (
    <EmptyState
      icon={<Lock aria-hidden="true" className="h-8 w-8" />}
      title={NO_PERMISSION_MESSAGE}
      description={
        <>
          {role ? (
            <>
              Your role is{' '}
              <span className="font-medium">{ROLE_LABELS[role] ?? role}</span>
              {permission ? (
                <>
                  , which does not include{' '}
                  <span className="font-mono text-xs">{permission}</span>
                </>
              ) : null}
              .{' '}
            </>
          ) : null}
          Ask an administrator if you need access.
        </>
      }
    />
  );
}

/**
 * Render children only when the user holds a capability.
 *
 * For a button or menu item, where an explanatory panel would be out of place.
 * The server enforces the same rule, so this is presentation only.
 */
export function IfPermitted({
  permission,
  children,
}: {
  readonly permission: Permission;
  readonly children: ReactNode;
}) {
  const { can } = useAuth();
  return can(permission) ? <>{children}</> : null;
}
