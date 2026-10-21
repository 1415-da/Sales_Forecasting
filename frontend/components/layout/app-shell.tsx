'use client';

import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

import { MobileNav, SidebarNav } from '@/components/layout/sidebar-nav';
import { Topbar } from '@/components/layout/topbar';
import { useAuth } from '@/hooks/use-auth';

/**
 * Routes rendered without the application frame.
 *
 * The public landing and sign-in pages have no navigation to show -- the nav
 * is derived from the signed-in user's permissions, and there is no user yet.
 */
const BARE_ROUTES = new Set(['/', '/login', '/register']);

/**
 * The application frame.
 *
 * A fixed sidebar on large screens; on small screens the nav moves inline
 * above the content rather than into a drawer, because a handful of
 * destinations does not justify the extra tap.
 */
export function AppShell({ children }: { readonly children: ReactNode }) {
  const pathname = usePathname();
  const { isAuthenticated, isChecking } = useAuth();

  // Bare while unauthenticated too: a shell with an empty sidebar around a
  // login form is worse than no shell.
  if (BARE_ROUTES.has(pathname) || (!isAuthenticated && !isChecking)) {
    return <>{children}</>;
  }

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <aside className="hidden w-64 shrink-0 border-r lg:block">
        <div className="sticky top-0 flex h-screen flex-col gap-6 overflow-y-auto p-4">
          <div className="px-3">
            <p className="text-sm font-semibold">Forecasting Platform</p>
            <p className="text-xs text-muted-foreground">Astro MLOps</p>
          </div>
          <SidebarNav />
          <p className="mt-auto px-3 text-xs text-muted-foreground">
            Forecasts are model estimates, not commitments.
          </p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <div className="border-b p-3 lg:hidden">
          <MobileNav />
        </div>
        <main className="min-w-0 flex-1 p-4 sm:p-6">{children}</main>
      </div>
    </div>
  );
}
