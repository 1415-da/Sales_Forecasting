'use client';

import { ChevronDown, LogOut, Settings as SettingsIcon, ShieldAlert } from 'lucide-react';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';
import { ROLE_LABELS, ROLE_TONE, type Role } from '@/lib/rbac';

/**
 * Who is signed in, and how to stop being signed in.
 *
 * The role is shown next to the name, not hidden in a submenu: when an action
 * is unavailable, "which role am I?" is the first thing a user needs, and
 * making them hunt for it turns a permissions question into a support ticket.
 */
export function UserMenu() {
  const { user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const container = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape, which a custom popover has to
  // re-earn; a native <details> would not give us the same layout control.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  if (!user) return null;

  const role = user.role as Role;
  const initials = user.full_name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('');

  return (
    <div ref={container} className="relative">
      <Button
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="gap-2"
      >
        <span
          aria-hidden="true"
          className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-[10px] font-semibold text-primary-foreground"
        >
          {initials || '?'}
        </span>
        <span className="hidden max-w-[12rem] truncate sm:inline">
          {user.full_name}
        </span>
        <ChevronDown aria-hidden="true" className="h-3.5 w-3.5 opacity-60" />
      </Button>

      {open ? (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-2 w-72 rounded-md border bg-popover p-1 shadow-lg"
        >
          <div className="space-y-1 border-b px-3 py-3">
            <p className="truncate text-sm font-medium">{user.full_name}</p>
            <p className="truncate text-xs text-muted-foreground">{user.email}</p>
            <div className="flex items-center gap-2 pt-1">
              <StatusBadge tone={ROLE_TONE[role] ?? 'neutral'}>
                {ROLE_LABELS[role] ?? user.role}
              </StatusBadge>
              {!user.is_active ? (
                <StatusBadge tone="danger">Deactivated</StatusBadge>
              ) : null}
            </div>
            <p className="pt-1 text-xs text-muted-foreground">
              {user.role_description}
            </p>
          </div>

          {user.must_change_password ? (
            <Link
              href="/settings?change-password=1"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="flex items-center gap-2 rounded px-3 py-2 text-sm text-amber-500 hover:bg-accent"
            >
              <ShieldAlert aria-hidden="true" className="h-4 w-4" />
              Change your password
            </Link>
          ) : null}

          <Link
            href="/settings"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 rounded px-3 py-2 text-sm hover:bg-accent"
          >
            <SettingsIcon aria-hidden="true" className="h-4 w-4" />
            Settings
          </Link>

          <button
            type="button"
            role="menuitem"
            disabled={signingOut}
            onClick={async () => {
              setSigningOut(true);
              await signOut();
            }}
            className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50"
          >
            <LogOut aria-hidden="true" className="h-4 w-4" />
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      ) : null}
    </div>
  );
}
