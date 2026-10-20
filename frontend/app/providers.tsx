'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import { isRetryableError } from '@/lib/utils/errors';

/**
 * Client-side providers.
 *
 * The QueryClient is created in state rather than at module scope so each
 * browser session gets its own cache and a server render never shares one
 * across requests.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Only transient failures are worth retrying; a 404 will not fix
            // itself, and retrying it just delays the error.
            retry: (failureCount, error) => failureCount < 2 && isRetryableError(error),
            refetchOnWindowFocus: false,
          },
          mutations: { retry: false },
        },
      }),
  );

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
