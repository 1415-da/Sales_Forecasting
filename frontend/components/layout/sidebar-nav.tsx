'use client';

import {
  Activity,
  FlaskConical,
  LineChart,
  ScrollText,
  Settings,
  ShieldCheck,
  Users,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useAuth } from '@/hooks/use-auth';
import { NAV_GROUPS, visibleNavEntries, type NavEntry } from '@/lib/rbac';
import { cn } from '@/lib/utils';

/**
 * Icon per destination.
 *
 * Keyed by href rather than carried in `NAV_ENTRIES`, so the RBAC module stays
 * free of React imports and can be used by tests and by non-UI code.
 */
const ICONS: Record<string, typeof LineChart> = {
  '/forecast': LineChart,
  '/scenario-planner': FlaskConical,
  '/monitoring': Activity,
  '/models': ShieldCheck,
  '/admin/users': Users,
  '/admin/audit-logs': ScrollText,
  '/settings': Settings,
};

/**
 * Role-aware primary navigation.
 *
 * Driven by one table in `lib/rbac`, so a page cannot be added to the nav
 * without declaring what it requires -- and a role is never shown a link to a
 * page the API would refuse.
 */
export function SidebarNav({ className }: { readonly className?: string }) {
  const pathname = usePathname();
  const { user } = useAuth();
  const entries = visibleNavEntries(user);

  if (entries.length === 0) return null;

  const grouped = NAV_GROUPS.map((group) => ({
    ...group,
    items: entries.filter((entry) => entry.group === group.id),
  })).filter((group) => group.items.length > 0);

  return (
    <nav aria-label="Primary" className={cn('space-y-5', className)}>
      {grouped.map((group) => (
        <div key={group.id} className="space-y-1">
          <p className="px-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {group.label}
          </p>
          {group.items.map((entry) => (
            <NavLink
              key={entry.href}
              entry={entry}
              pathname={pathname}
            />
          ))}
        </div>
      ))}
    </nav>
  );
}

function NavLink({
  entry,
  pathname,
}: {
  readonly entry: NavEntry;
  readonly pathname: string;
}) {
  // Prefix match so a nested route keeps its parent highlighted, but not for
  // the root-like entries where it would match everything.
  const isActive =
    pathname === entry.href || pathname.startsWith(`${entry.href}/`);
  const Icon = ICONS[entry.href] ?? LineChart;

  return (
    <Link
      href={entry.href}
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
        <span className="block font-medium leading-tight">{entry.label}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {entry.description}
        </span>
      </span>
    </Link>
  );
}

/** Compact horizontal nav for small screens. */
export function MobileNav() {
  const pathname = usePathname();
  const { user } = useAuth();
  const entries = visibleNavEntries(user);

  if (entries.length === 0) return null;

  return (
    <nav
      aria-label="Primary"
      className="flex gap-2 overflow-x-auto pb-1"
    >
      {entries.map((entry) => {
        const isActive = pathname === entry.href;
        const Icon = ICONS[entry.href] ?? LineChart;
        return (
          <Link
            key={entry.href}
            href={entry.href}
            aria-current={isActive ? 'page' : undefined}
            className={cn(
              'flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors',
              isActive
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/50',
            )}
          >
            <Icon aria-hidden="true" className="h-4 w-4" />
            {entry.label}
          </Link>
        );
      })}
    </nav>
  );
}
