'use client';

import { Loader2, Monitor, ShieldCheck } from 'lucide-react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { LOGIN_PATH, useAuth } from '@/hooks/use-auth';
import { usePermissionMatrix } from '@/hooks/use-admin';
import { apiBaseUrl, changePassword, getSessions } from '@/lib/api';
import { formatRelative, formatTimestamp } from '@/lib/format';
import { ROLE_LABELS, ROLE_TONE, type Role } from '@/lib/rbac';
import type { SessionSummary } from '@/lib/types/auth';

/** Mirrors the server policy in `api.core.security`. */
const PASSWORD_RULES: readonly { label: string; test: (value: string) => boolean }[] = [
  { label: 'At least 12 characters', test: (value) => value.length >= 12 },
  { label: 'A lowercase letter', test: (value) => /[a-z]/.test(value) },
  { label: 'An uppercase letter', test: (value) => /[A-Z]/.test(value) },
  { label: 'A digit', test: (value) => /\d/.test(value) },
  { label: 'A symbol', test: (value) => /[^A-Za-z0-9]/.test(value) },
];

/**
 * `Settings` reads `useSearchParams` for the `change-password` deep link, which
 * forces a client-side bailout during prerender; the Suspense boundary is what
 * keeps `next build` able to emit this route.
 */
export default function SettingsPage() {
  return (
    <RequireAuth>
      <Suspense
        fallback={<p className="text-sm text-muted-foreground">Loading settings…</p>}
      >
        <Settings />
      </Suspense>
    </RequireAuth>
  );
}

function Settings() {
  const { user, signOut } = useAuth();
  const params = useSearchParams();
  const mustChange = params.get('change-password') === '1';

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Your profile, password, and sessions.
        </p>
      </div>

      {user?.must_change_password ? (
        <div className="flex items-start gap-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4">
          <ShieldCheck
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 text-amber-500"
          />
          <p className="text-sm">
            Your password was set by an administrator or generated at
            bootstrap. Change it below before using the platform.
          </p>
        </div>
      ) : null}

      <ProfileCard />
      <PasswordCard autoFocus={mustChange} onDone={() => void signOut()} />
      <SessionsCard />
      <PermissionsCard />
      <EnvironmentCard />
    </div>
  );
}

function ProfileCard() {
  const { user } = useAuth();
  if (!user) return null;
  const role = user.role as Role;

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Profile</CardTitle>
        <CardDescription>
          Your name and role are managed by an administrator.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
          <Field label="Name" value={user.full_name} />
          <Field label="Email" value={user.email} />
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">Role</dt>
            <dd>
              <StatusBadge tone={ROLE_TONE[role] ?? 'neutral'}>
                {ROLE_LABELS[role] ?? user.role}
              </StatusBadge>
            </dd>
          </div>
          <Field
            label="Last sign-in"
            value={user.last_login_at ? formatRelative(user.last_login_at) : '—'}
          />
          <div className="sm:col-span-2">
            <p className="text-xs text-muted-foreground">{user.role_description}</p>
          </div>
        </dl>
      </CardContent>
    </Card>
  );
}

function PasswordCard({
  autoFocus,
  onDone,
}: {
  readonly autoFocus: boolean;
  readonly onDone: () => void;
}) {
  const router = useRouter();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  const unmet = PASSWORD_RULES.filter((rule) => !rule.test(next));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);

    if (unmet.length > 0) {
      setError(new Error('The new password does not meet all requirements.'));
      return;
    }
    if (next !== confirm) {
      setError(new Error('The two new passwords do not match.'));
      return;
    }

    setSubmitting(true);
    try {
      await changePassword({ current_password: current, new_password: next });
      setDone(true);
      // The server revokes every session, including this one, so staying on
      // the page would leave a client holding a token the API has invalidated.
      setTimeout(() => {
        onDone();
        router.replace(LOGIN_PATH);
      }, 2500);
    } catch (caught) {
      setError(caught);
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <Card>
        <CardContent className="space-y-2 pt-6">
          <p className="text-sm font-medium">Password changed.</p>
          <p className="text-sm text-muted-foreground">
            Every session has been revoked. Signing you out…
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Change password</CardTitle>
        <CardDescription>
          All of your sessions are revoked, so you will be signed out and will
          need to sign in again.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="max-w-md space-y-4" onSubmit={submit} noValidate>
          <div className="space-y-2">
            <Label htmlFor="current-password">Current password</Label>
            <Input
              id="current-password"
              type="password"
              autoComplete="current-password"
              autoFocus={autoFocus}
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="new-password">New password</Label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
            <ul className="space-y-0.5 pt-1">
              {PASSWORD_RULES.map((rule) => {
                const met = rule.test(next);
                return (
                  <li
                    key={rule.label}
                    className={met ? 'text-xs text-emerald-500' : 'text-xs text-muted-foreground'}
                  >
                    {met ? '✓' : '•'} {rule.label}
                  </li>
                );
              })}
            </ul>
          </div>
          <div className="space-y-2">
            <Label htmlFor="confirm-password">Confirm new password</Label>
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </div>

          {error ? <ErrorState error={error} /> : null}

          <Button type="submit" disabled={submitting || !current || !next}>
            {submitting ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Changing…
              </>
            ) : (
              'Change password'
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function SessionsCard() {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const { signOut } = useAuth();

  useEffect(() => {
    let cancelled = false;
    getSessions()
      .then((rows) => {
        if (!cancelled) setSessions(rows);
      })
      .catch((caught) => {
        if (!cancelled) setError(caught);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Active sessions</CardTitle>
        <CardDescription>
          Where your account is currently signed in.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? <ErrorState error={error} /> : null}
        {sessions === null && !error ? (
          <p className="text-sm text-muted-foreground">Loading sessions…</p>
        ) : null}
        {sessions?.length === 0 ? (
          <p className="text-sm text-muted-foreground">No recorded sessions.</p>
        ) : null}
        {sessions?.map((session) => (
          <div
            key={session.id}
            className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-xs"
          >
            <div className="min-w-0 space-y-0.5">
              <p className="font-medium">
                {session.ip_address ?? 'unknown address'}
                {session.current ? (
                  <StatusBadge tone="success" className="ml-2">
                    this session
                  </StatusBadge>
                ) : null}
              </p>
              <p className="truncate text-muted-foreground">
                {session.user_agent ?? 'unknown client'}
              </p>
            </div>
            <p className="text-muted-foreground">
              started {formatRelative(session.created_at)} · expires{' '}
              {formatTimestamp(session.expires_at)}
            </p>
          </div>
        ))}
        {sessions && sessions.length > 1 ? (
          <Button size="sm" variant="outline" onClick={() => void signOut(true)}>
            Sign out everywhere
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

function PermissionsCard() {
  const { user } = useAuth();
  // The matrix endpoint is ADMIN-only, so it is only requested when the user
  // can actually read it; everyone else sees their own permission list.
  const isAdmin = user?.role === 'ADMIN';
  const matrix = usePermissionMatrix(isAdmin);

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Your permissions</CardTitle>
        <CardDescription>
          What your role allows. Enforced by the API on every request.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-1.5">
          {(user?.permissions ?? []).map((permission) => (
            <StatusBadge key={permission} tone="neutral">
              {permission.replace(/_/g, ' ')}
            </StatusBadge>
          ))}
        </div>
        {isAdmin && matrix.data ? (
          <details className="rounded-md border p-3">
            <summary className="cursor-pointer text-sm font-medium">
              Full role matrix ({matrix.data.roles.length} roles)
            </summary>
            <div className="mt-3 space-y-3">
              {matrix.data.roles.map((role) => (
                <div key={role.role} className="space-y-1">
                  <p className="text-sm font-medium">{role.role}</p>
                  <p className="text-xs text-muted-foreground">{role.description}</p>
                  <p className="font-mono text-[11px] text-muted-foreground">
                    {role.permissions.join(', ')}
                  </p>
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </CardContent>
    </Card>
  );
}

function EnvironmentCard() {
  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle className="flex items-center gap-2">
          <Monitor aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
          Environment
        </CardTitle>
        <CardDescription>
          The API this browser talks to. It is the only backend the browser
          reaches.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 gap-y-2 text-xs">
          <Field label="API base URL" value={apiBaseUrl() || 'same origin'} />
          {/* Deliberately nothing about MLflow, MinIO, or the database: the
              browser is never given infrastructure addresses or credentials. */}
          <Field label="Theme" value="Dark (fixed)" />
        </dl>
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="break-all text-right font-medium">{value}</dd>
    </div>
  );
}
