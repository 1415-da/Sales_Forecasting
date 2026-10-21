'use client';

import { Loader2, UserPlus } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { EmptyState } from '@/components/shared/empty-state';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { POST_LOGIN_PATH, useAuth } from '@/hooks/use-auth';
import { ApiError, friendlyMessage, getAuthConfig } from '@/lib/api';

/** Mirrors the server policy in `api.core.security`, for pre-submit feedback. */
const PASSWORD_RULES: readonly { readonly label: string; readonly test: (value: string) => boolean }[] = [
  { label: 'At least 12 characters', test: (value) => value.length >= 12 },
  { label: 'A lowercase letter', test: (value) => /[a-z]/.test(value) },
  { label: 'An uppercase letter', test: (value) => /[A-Z]/.test(value) },
  { label: 'A digit', test: (value) => /\d/.test(value) },
  { label: 'A symbol', test: (value) => /[^A-Za-z0-9]/.test(value) },
];

/**
 * Self-registration page.
 *
 * Only reachable when the deployment enables it. The role is not a field:
 * the server assigns it, because a form that let someone choose their own
 * privilege level would not be an access-control system.
 */
export default function RegisterPage() {
  const router = useRouter();
  const { signUp, isAuthenticated, isChecking } = useAuth();

  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isChecking && isAuthenticated) router.replace(POST_LOGIN_PATH);
  }, [isAuthenticated, isChecking, router]);

  useEffect(() => {
    let cancelled = false;
    getAuthConfig()
      .then((config) => {
        if (!cancelled) setEnabled(config.self_registration_enabled);
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
      });
      router.replace(POST_LOGIN_PATH);
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
                  href="/login"
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
            Your access level is assigned by an administrator after sign-up.
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
              <Link href="/login" className="font-medium underline">
                Sign in
              </Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
