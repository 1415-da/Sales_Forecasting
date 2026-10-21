import type { ReactNode } from 'react';

import { SidebarNav } from '@/components/layout/sidebar-nav';
import { Topbar } from '@/components/layout/topbar';

/**
 * The application frame.
 *
 * A fixed sidebar on large screens; on small screens the nav moves inline above
 * the content rather than into a drawer, because with two destinations a drawer
 * costs a tap and saves nothing.
 */
export function AppShell({ children }: { readonly children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <aside className="hidden w-64 shrink-0 border-r lg:block">
        <div className="sticky top-0 flex h-screen flex-col gap-6 p-4">
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
          <SidebarNav className="flex gap-2 overflow-x-auto" />
        </div>
        <main className="min-w-0 flex-1 p-4 sm:p-6">{children}</main>
      </div>
    </div>
  );
}
