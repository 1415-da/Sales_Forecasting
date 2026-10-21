'use client';

import { Activity, ShieldAlert } from 'lucide-react';
import Link from 'next/link';

import { UserMenu } from '@/components/layout/user-menu';
import { StatusBadge, healthTone } from '@/components/shared/status-badge';
import { useAuth } from '@/hooks/use-auth';
import { useHealth } from '@/hooks/use-models';

/**
 * Backend state and the current user, always visible.
 *
 * API health is the single most useful thing to show at the top of an ML
 * console: when something fails, the first question is whether the API and the
 * registry are up, and answering it here saves the user from guessing.
 */
export function Topbar() {
  const { isAuthenticated, user } = useAuth();
  // Only polled once signed in: health requires no auth, but an unauthenticated
  // visitor has nothing to do with the answer.
  const { data, isLoading, isError } = useHealth();

  const tone = isError
    ? 'danger'
    : isLoading
      ? 'neutral'
      : healthTone(data?.status ?? '');
  const label = isError
    ? 'API unreachable'
    : isLoading
      ? 'Checking…'
      : `API ${data?.status ?? 'unknown'}`;

  const model = data?.production_model;

  return (
    <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b bg-background/95 px-4 py-3 backdrop-blur sm:px-6">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <Activity aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="truncate text-sm font-medium">Sales Forecasting</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {user?.must_change_password ? (
          <Link href="/settings?change-password=1">
            <StatusBadge tone="warning" className="cursor-pointer">
              <ShieldAlert aria-hidden="true" className="mr-1 inline h-3 w-3" />
              Password change required
            </StatusBadge>
          </Link>
        ) : null}
        {model ? (
          <StatusBadge tone="info">
            {model.model} v{model.version} · {model.stage}
          </StatusBadge>
        ) : null}
        <StatusBadge tone={tone}>{label}</StatusBadge>
        {isAuthenticated ? <UserMenu /> : null}
      </div>
    </header>
  );
}
