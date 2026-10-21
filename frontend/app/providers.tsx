'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import { ToastProvider } from '@/components/ui/toast';
import { AuthProvider } from '@/hooks/use-auth';
import { isAutoRetryableError } from '@/lib/utils/errors';

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
            // Only transport-level and upstream-unavailable failures retry; a
            // 404 or 422 will not fix itself, and retrying it just delays the
            // error the user needs to see.
            retry: (failureCount, error) => failureCount < 2 && isAutoRetryableError(error),
            refetchOnWindowFocus: false,
          },
          mutations: { retry: false },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      {/*
        AuthProvider sits inside QueryClientProvider: it establishes the
        session, and every query depends on that having happened. It sits
        outside ToastProvider so a sign-out can surface a toast.
      */}
      <AuthProvider>
        <ToastProvider>{children}</ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
