'use client';

import { FlaskConical, LineChart } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { cn } from '@/lib/utils';

interface NavItem {
  readonly href: string;
  readonly label: string;
  readonly description: string;
  readonly icon: typeof LineChart;
}

const NAV_ITEMS: readonly NavItem[] = [
  {
    href: '/forecast',
    label: 'Forecast Console',
    description: 'Generate a projection',
    icon: LineChart,
  },
  {
    href: '/scenario-planner',
    label: 'Scenario Planner',
    description: 'Predictive what-if',
    icon: FlaskConical,
  },
];

/** Primary navigation. Only the pages that exist are listed. */
export function SidebarNav({ className }: { readonly className?: string }) {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary" className={cn('space-y-1', className)}>
      {NAV_ITEMS.map((item) => {
        // Prefix match so a future nested route keeps its parent highlighted.
        const isActive = pathname === item.href || pathname.startsWith(`${item.href}/`);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={isActive ? 'page' : undefined}
            className={cn(
              'flex items-start gap-3 rounded-md px-3 py-2 text-sm transition-colors',
              isActive
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
            )}
          >
            <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="min-w-0">
              <span className="block font-medium leading-tight">{item.label}</span>
              <span className="block truncate text-xs text-muted-foreground">
                {item.description}
              </span>
            </span>
          </Link>
        );
      })}
    </nav>
  );
}
