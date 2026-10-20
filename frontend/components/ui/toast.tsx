'use client';

import * as React from 'react';
import { CheckCircle2, X, XCircle } from 'lucide-react';

import { cn } from '@/lib/utils';
import { setToastSink, type ErrorPresentation } from '@/lib/utils/errors';

export type ToastVariant = 'success' | 'error' | 'warning';

export interface Toast {
  id: string;
  title: string;
  description?: string;
  variant: ToastVariant;
  /** Correlation id, shown so a user can quote it to support. */
  requestId?: string | null;
}

interface ToastContextValue {
  toasts: Toast[];
  toast: (toast: Omit<Toast, 'id'>) => string;
  dismiss: (id: string) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

const AUTO_DISMISS_MS = 6000;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([]);

  const dismiss = React.useCallback((id: string) => {
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);

  const toast = React.useCallback(
    (input: Omit<Toast, 'id'>) => {
      const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      setToasts((current) => [...current, { ...input, id }]);
      // Errors stay until dismissed: they usually carry a request id someone
      // may need to copy.
      if (input.variant === 'success') {
        setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
      }
      return id;
    },
    [dismiss],
  );

  const value = React.useMemo(() => ({ toasts, toast, dismiss }), [toasts, toast, dismiss]);

  // Route data-layer failures into the same surface, so an API error raised
  // deep in a hook is announced the same way as a local one.
  React.useEffect(() => {
    setToastSink((presentation: ErrorPresentation) => {
      toast({
        title: presentation.title,
        description: presentation.message,
        variant: presentation.severity === 'warning' ? 'warning' : 'error',
        requestId: presentation.requestId,
      });
    });
    return () => setToastSink(null);
  }, [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <Toaster />
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = React.useContext(ToastContext);
  if (!context) throw new Error('useToast must be used inside <ToastProvider>');
  return context;
}

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  success:
    'border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-100',
  error:
    'border-destructive/40 bg-background text-foreground',
  warning:
    'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100',
};

/** Live region holding the toast stack. */
export function Toaster() {
  const { toasts, dismiss } = useToast();

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-2"
    >
      {toasts.map((item) => {
        const Icon = item.variant === 'success' ? CheckCircle2 : XCircle;
        return (
          <div
            key={item.id}
            role={item.variant === 'success' ? 'status' : 'alert'}
            className={cn(
              'pointer-events-auto flex items-start gap-2 rounded-md border p-3 text-sm shadow-lg',
              VARIANT_CLASSES[item.variant],
            )}
          >
            <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="min-w-0 flex-1 space-y-0.5">
              <p className="font-medium">{item.title}</p>
              {item.description ? (
                <p className="text-xs opacity-90">{item.description}</p>
              ) : null}
              {item.requestId ? (
                <p className="font-mono text-[11px] opacity-75">{item.requestId}</p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => dismiss(item.id)}
              aria-label="Dismiss notification"
              className="rounded-sm opacity-60 transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
