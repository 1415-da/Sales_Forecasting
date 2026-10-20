import type { ReactNode } from 'react';

import { Providers } from '@/app/providers';

import './globals.css';

export const metadata = {
  title: 'Sales Forecasting',
  description: 'Forecasting and scenario planning for retail sales.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
