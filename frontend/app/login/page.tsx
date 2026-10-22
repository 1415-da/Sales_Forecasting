'use client';

import { Loader2, LogIn } from 'lucide-react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { POST_LOGIN_PATH, useAuth } from '@/hooks/use-auth';
import { ApiError, friendlyMessage, loadAuthConfig } from '@/lib/api';

/**
 * Sign-in page.
 *
 * Deliberately says nothing about *which* half of the credentials was wrong:
 * the API returns one 401 for an unknown address and a wrong password, and
 * reflecting a distinction the server does not make would reintroduce the
 * account-enumeration leak it avoids.
 */
/**
 * `useSearchParams` forces a client-side bailout during prerender, so the part
 * that reads it lives inside a Suspense boundary. Without one, `next build`
 * fails on this route rather than shipping it.
 */
export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginForm />
    </Suspense>
  );
}

function LoginFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-2">
          <CardTitle className="text-lg">Sign in</CardTitle>
          <CardDescription>Loading…</CardDescription>
        </CardHeader>
      </Card>
    </div>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const { signIn, isAuthenticated, isChecking } = useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [registrationOpen, setRegistrationOpen] = useState(false);

  // Where to land after signing in: the page the user was trying to reach,
  // which `RequireAuth` put in the query string.
  const next = params.get('next') || POST_LOGIN_PATH;

  // Already signed in (a bookmarked /login, or a second tab) -- go straight
  // through rather than asking for a password that is not needed.
  useEffect(() => {
    if (!isChecking && isAuthenticated) router.replace(next);
  }, [isAuthenticated, isChecking, next, router]);

  // Hide the registration link when the deployment has self-registration off,
  // rather than offering a form that can only ever fail.
  useEffect(() => {
    let cancelled = false;
    loadAuthConfig()
      .then((config) => {
        if (!cancelled) setRegistrationOpen(config.self_registration_enabled);
      })
      .catch(() => {
        // Unknown is not "disabled": keep the link, and let the sign-up page
        // explain if the service is unreachable.
        if (!cancelled) setRegistrationOpen(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);

    if (!email.trim() || !password) {
      setError('Enter your email address and password.');
      return;
    }

    setSubmitting(true);
    try {
      const user = await signIn(email.trim(), password);
      // A generated or admin-set password must be changed before anything
      // else, so route there instead of to the console.
      router.replace(user.must_change_password ? '/settings?change-password=1' : next);
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? friendlyMessage(caught)
          : 'Could not sign in. Try again.';
      // The usual cause for a first-time visitor is that no account exists yet.
      setError(
        registrationOpen &&
          caught instanceof ApiError &&
          caught.code === 'INVALID_CREDENTIALS'
          ? `${message} New here? Create an account first.`
          : message,
      );
      // Never leave the password in the field after a failure: the next
      // attempt should be deliberate.
      setPassword('');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-2">
          <CardTitle className="text-lg">Sign in</CardTitle>
          <CardDescription>
            Sales Forecasting Platform. Forecasts, scenarios, and model
            operations.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={submit} noValidate>
            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                name="email"
                type="email"
                autoComplete="username"
                autoFocus
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                aria-invalid={error ? true : undefined}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-invalid={error ? true : undefined}
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
                  Signing in…
                </>
              ) : (
                <>
                  <LogIn aria-hidden="true" className="h-4 w-4" />
                  Sign in
                </>
              )}
            </Button>

            {registrationOpen ? (
              <p className="text-center text-sm text-muted-foreground">
                No account?{' '}
                <Link
                  href={
                    params.get('next')
                      ? `/register?next=${encodeURIComponent(params.get('next')!)}`
                      : '/register'
                  }
                  className="font-medium underline"
                >
                  Create one
                </Link>
              </p>
            ) : (
              <p className="text-center text-xs text-muted-foreground">
                Accounts are created by an administrator.
              </p>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
