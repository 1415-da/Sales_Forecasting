'use client';

import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * Tabs with shadcn's API, implemented with roving tabindex.
 *
 * Only the active tab is in the tab order; arrow keys move between tabs and
 * Home/End jump to the ends, which is what the WAI-ARIA tabs pattern expects.
 */
interface TabsContextValue {
  value: string;
  setValue: (value: string) => void;
  baseId: string;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabs(component: string): TabsContextValue {
  const context = React.useContext(TabsContext);
  if (!context) throw new Error(`${component} must be used inside <Tabs>`);
  return context;
}

export interface TabsProps {
  defaultValue: string;
  value?: string;
  onValueChange?: (value: string) => void;
  className?: string;
  children: React.ReactNode;
}

function Tabs({ defaultValue, value, onValueChange, className, children }: TabsProps) {
  const [internal, setInternal] = React.useState(defaultValue);
  const baseId = React.useId();
  const current = value ?? internal;

  const setValue = React.useCallback(
    (next: string) => {
      if (value === undefined) setInternal(next);
      onValueChange?.(next);
    },
    [onValueChange, value],
  );

  const context = React.useMemo(
    () => ({ value: current, setValue, baseId }),
    [current, setValue, baseId],
  );

  return (
    <TabsContext.Provider value={context}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

const TabsList = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { 'aria-label': string }
>(({ className, onKeyDown, ...props }, ref) => (
  <div
    ref={ref}
    role="tablist"
    className={cn(
      'inline-flex h-9 items-center justify-start gap-1 rounded-md bg-muted p-1 text-muted-foreground',
      className,
    )}
    onKeyDown={(event) => {
      const keys = ['ArrowRight', 'ArrowLeft', 'Home', 'End'];
      if (!keys.includes(event.key)) {
        onKeyDown?.(event);
        return;
      }
      const tabs = Array.from(
        event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)'),
      );
      const index = tabs.findIndex((tab) => tab === document.activeElement);
      if (index < 0) return;

      event.preventDefault();
      const target =
        event.key === 'Home'
          ? tabs[0]
          : event.key === 'End'
            ? tabs[tabs.length - 1]
            : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
      target?.focus();
      target?.click();
    }}
    {...props}
  />
));
TabsList.displayName = 'TabsList';

export interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string;
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value, ...props }, ref) => {
    const tabs = useTabs('TabsTrigger');
    const selected = tabs.value === value;
    return (
      <button
        ref={ref}
        type="button"
        role="tab"
        id={`${tabs.baseId}-tab-${value}`}
        aria-selected={selected}
        aria-controls={`${tabs.baseId}-panel-${value}`}
        tabIndex={selected ? 0 : -1}
        onClick={() => tabs.setValue(value)}
        className={cn(
          'inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50',
          selected && 'bg-background text-foreground shadow-sm',
          className,
        )}
        {...props}
      />
    );
  },
);
TabsTrigger.displayName = 'TabsTrigger';

export interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string;
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value, ...props }, ref) => {
    const tabs = useTabs('TabsContent');
    if (tabs.value !== value) return null;
    return (
      <div
        ref={ref}
        role="tabpanel"
        id={`${tabs.baseId}-panel-${value}`}
        aria-labelledby={`${tabs.baseId}-tab-${value}`}
        tabIndex={0}
        className={cn('mt-3 focus-visible:outline-none', className)}
        {...props}
      />
    );
  },
);
TabsContent.displayName = 'TabsContent';

export { Tabs, TabsContent, TabsList, TabsTrigger };
