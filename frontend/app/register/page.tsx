'use client';

import { Loader2, UserPlus } from 'lucide-react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState } from 'react';

import { EmptyState } from '@/components/shared/empty-state';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { POST_LOGIN_PATH, useAuth } from '@/hooks/use-auth';
import { ApiError, friendlyMessage, getAuthConfig } from '@/lib/api';
import { ROLES, ROLE_DESCRIPTIONS, ROLE_LABELS, type Role } from '@/lib/rbac';
import { cn } from '@/lib/utils';

/** Mirrors the server policy in `api.core.security`, for pre-submit feedback. */
const PASSWORD_RULES: readonly { readonly label: string; readonly test: (value: string) => boolean }[] = [
  { label: 'At least 12 characters', test: (value) => value.length >= 12 },
  { label: 'A lowercase letter', test: (value) => /[a-z]/.test(value) },
  { label: 'An uppercase letter', test: (value) => /[A-Z]/.test(value) },
  { label: 'A digit', test: (value) => /\d/.test(value) },
  { label: 'A symbol', test: (value) => /[^A-Za-z0-9]/.test(value) },
];

/**
 * Self-registration page, and where a signed-out visitor lands.
 *
 * Only usable when the deployment enables it. The role is not a field:
 * the server assigns it, because a form that let someone choose their own
 * privilege level would not be an access-control system.
 *
 * `useSearchParams` needs a Suspense boundary, as on the sign-in page.
 */
export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <RegisterForm />
    </Suspense>
  );
}

function RegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { signUp, isAuthenticated, isChecking } = useAuth();

  // The page `RequireAuth` was protecting, carried through sign-up or sign-in.
  const nextParam = params.get('next');
  const next = nextParam || POST_LOGIN_PATH;
  const loginHref = nextParam ? `/login?next=${encodeURIComponent(nextParam)}` : '/login';

  const [enabled, setEnabled] = useState<boolean | null>(null);
  // Offered by the server, least privileged first. The server re-checks the
  // choice, so this list is presentation, not the control.
  const [offeredRoles, setOfferedRoles] = useState<Role[]>([]);
  const [role, setRole] = useState<Role | ''>('');
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isChecking && isAuthenticated) router.replace(next);
  }, [isAuthenticated, isChecking, next, router]);

  useEffect(() => {
    let cancelled = false;
    getAuthConfig()
      .then((config) => {
        if (cancelled) return;
        setEnabled(config.self_registration_enabled);
        const offered = config.self_registration_roles.filter((item): item is Role =>
          (ROLES as readonly string[]).includes(item),
        );
        setOfferedRoles(offered);
        const fallback = config.default_self_registration_role;
        setRole(
          offered.find((item) => item === fallback) ?? offered[0] ?? '',
        );
      })
      .catch(() => {
        if (!cancelled) setEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const unmetRules = PASSWORD_RULES.filter((rule) => !rule.test(password));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);

    if (!fullName.trim() || !email.trim()) {
      setError('Enter your name and email address.');
      return;
    }
    if (unmetRules.length > 0) {
      setError('The password does not meet all of the requirements.');
      return;
    }
    if (password !== confirm) {
      setError('The two passwords do not match.');
      return;
    }

    setSubmitting(true);
    try {
      await signUp({
        email: email.trim(),
        full_name: fullName.trim(),
        password,
        ...(role ? { role } : {}),
      });
      router.replace(next);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? friendlyMessage(caught)
          : 'Could not create the account. Try again.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (enabled === false) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardContent className="pt-6">
            <EmptyState
              title="Self-registration is disabled"
              description="Accounts on this deployment are created by an administrator."
              action={
                <Link
                  href={loginHref}
                  className="text-sm font-medium underline underline-offset-4"
                >
                  Back to sign in
                </Link>
              }
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-2">
          <CardTitle className="text-lg">Create an account</CardTitle>
          <CardDescription>
            New to Sales Forecasting? Create an account and choose the role
            that matches your work. An administrator can change it later.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={submit} noValidate>
            <div className="space-y-2">
              <Label htmlFor="full_name">Full name</Label>
              <Input
                id="full_name"
                autoComplete="name"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>

            {offeredRoles.length > 0 ? (
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium leading-none">Role</legend>
                <div className="space-y-2 pt-1">
                  {offeredRoles.map((item) => (
                    <label
                      key={item}
                      htmlFor={`role-${item}`}
                      className={cn(
                        'flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors',
                        role === item ? 'border-primary bg-accent' : 'hover:bg-accent/50',
                      )}
                    >
                      <input
                        id={`role-${item}`}
                        type="radio"
                        name="role"
                        value={item}
                        checked={role === item}
                        onChange={() => setRole(item)}
                        className="mt-0.5"
                      />
                      <span className="space-y-0.5">
                        <span className="block text-sm font-medium">{ROLE_LABELS[item]}</span>
                        <span className="block text-xs text-muted-foreground">
                          {ROLE_DESCRIPTIONS[item]}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ) : null}

            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <ul className="space-y-0.5 pt-1">
                {PASSWORD_RULES.map((rule) => {
                  const met = rule.test(password);
                  return (
                    <li
                      key={rule.label}
                      className={
                        met
                          ? 'text-xs text-emerald-500'
                          : 'text-xs text-muted-foreground'
                      }
                    >
                      {met ? '✓' : '•'} {rule.label}
                    </li>
                  );
                })}
              </ul>
            </div>

            <div className="space-y-2">
              <Label htmlFor="confirm">Confirm password</Label>
              <Input
                id="confirm"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
              />
            </div>

            {error ? (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            ) : null}

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? (
                <>
                  <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                  Creating account…
                </>
              ) : (
                <>
                  <UserPlus aria-hidden="true" className="h-4 w-4" />
                  Create account
                </>
              )}
            </Button>

            <p className="text-center text-sm text-muted-foreground">
              Already have an account?{' '}
              <Link href={loginHref} className="font-medium underline">
                Sign in
              </Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
