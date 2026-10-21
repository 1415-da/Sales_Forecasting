import type { ReactNode } from 'react';

import { AppShell } from '@/components/layout/app-shell';
import { Providers } from '@/app/providers';

import './globals.css';

export const metadata = {
  title: 'Sales Forecasting',
  description: 'Forecasting and scenario planning for retail sales.',
};

/**
 * Root layout.
 *
 * `class="dark"` is set on <html> rather than left to a preference hook: the
 * design tokens are tuned dark-first, and a flash of the light palette on every
 * load is worse than not offering a toggle yet.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
