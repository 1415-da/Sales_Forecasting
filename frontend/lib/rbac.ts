/**
 * Roles and permissions, mirrored from the API.
 *
 * Presentation only. The server re-checks every request, so nothing here is a
 * security control -- its job is to avoid showing someone a button that will
 * only ever return 403.
 *
 * The permission *names* are duplicated from `api/core/rbac.py`, but the
 * *grants* are not: `/auth/me` returns the signed-in user's permission list,
 * and that is what `can()` reads. So a change to the matrix on the server
 * takes effect in the UI without a frontend release, and the two cannot
 * silently disagree about what a role may do.
 */
import { z } from 'zod';

export const ROLES = [
  'ADMIN',
  'ML_ENGINEER',
  'ANALYST',
  'BUSINESS_USER',
  'VIEWER',
] as const;

export type Role = (typeof ROLES)[number];

export const roleSchema = z.enum(ROLES);

/** Capability names, matching `api.core.rbac.Permission`. */
export const PERMISSIONS = {
  viewForecast: 'view_forecast',
  generateForecast: 'generate_forecast',
  exportForecast: 'export_forecast',
  runScenario: 'run_scenario',
  viewScenario: 'view_scenario',
  viewModelPerformance: 'view_model_performance',
  viewModelRegistry: 'view_model_registry',
  viewModelRegistryDetail: 'view_model_registry_detail',
  promoteModel: 'promote_model',
  archiveModel: 'archive_model',
  rollbackModel: 'rollback_model',
  triggerRetraining: 'trigger_retraining',
  viewMonitoring: 'view_monitoring',
  viewMonitoringDetail: 'view_monitoring_detail',
  manageAlerts: 'manage_alerts',
  manageUsers: 'manage_users',
  viewAuditLogs: 'view_audit_logs',
  viewAllAuditLogs: 'view_all_audit_logs',
} as const;

export type Permission = (typeof PERMISSIONS)[keyof typeof PERMISSIONS];

/** Display label for a role. */
export const ROLE_LABELS: Record<Role, string> = {
  ADMIN: 'Administrator',
  ML_ENGINEER: 'ML Engineer',
  ANALYST: 'Analyst',
  BUSINESS_USER: 'Business User',
  VIEWER: 'Viewer',
};

/** One-line description of what a role can do. */
export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  ADMIN: 'Full access, including user management and audit logs.',
  ML_ENGINEER:
    'Promote, archive, and roll back models; trigger retraining; manage alerts; view audit logs.',
  ANALYST:
    'Generate forecasts, run scenarios, and read detailed model and monitoring data.',
  BUSINESS_USER:
    'Generate forecasts, run scenarios, and read high-level model health.',
  VIEWER: 'Read-only access to forecasts, performance, and monitoring.',
};

/** Tone for a role badge; the privileged roles are visually distinct. */
export const ROLE_TONE: Record<Role, 'success' | 'info' | 'warning' | 'neutral'> = {
  ADMIN: 'warning',
  ML_ENGINEER: 'info',
  ANALYST: 'success',
  BUSINESS_USER: 'neutral',
  VIEWER: 'neutral',
};

/** The shape `can()` needs: anything carrying a permission list. */
export interface PermissionHolder {
  readonly permissions: readonly string[];
}

/**
 * Whether the signed-in user holds a capability.
 *
 * Reads the list the server sent rather than a local role table, so the UI
 * cannot drift from what the API actually enforces.
 */
export function can(
  user: PermissionHolder | null | undefined,
  permission: Permission,
): boolean {
  if (!user) return false;
  return user.permissions.includes(permission);
}

/** Whether the user holds every listed capability. */
export function canAll(
  user: PermissionHolder | null | undefined,
  ...permissions: Permission[]
): boolean {
  return permissions.every((permission) => can(user, permission));
}

/** Whether the user holds at least one of the listed capabilities. */
export function canAny(
  user: PermissionHolder | null | undefined,
  ...permissions: Permission[]
): boolean {
  return permissions.some((permission) => can(user, permission));
}

/** The message shown wherever an action is unavailable. */
export const NO_PERMISSION_MESSAGE =
  'You do not have permission to perform this action.';

/** A navigation destination and what it requires. */
export interface NavEntry {
  readonly href: string;
  readonly label: string;
  readonly description: string;
  /** Any one of these is enough to see the entry. */
  readonly requires: readonly Permission[];
  readonly group: 'forecasting' | 'operations' | 'administration';
}

/**
 * Every page, with its requirement.
 *
 * One table drives the sidebar, so a new page cannot be added to the nav
 * without declaring what it needs, and a role never sees a link to a page the
 * API would refuse.
 */
export const NAV_ENTRIES: readonly NavEntry[] = [
  {
    href: '/forecast',
    label: 'Forecast Console',
    description: 'Generate a projection',
    requires: [PERMISSIONS.viewForecast],
    group: 'forecasting',
  },
  {
    href: '/scenario-planner',
    label: 'Scenario Planner',
    description: 'Predictive what-if',
    requires: [PERMISSIONS.viewScenario],
    group: 'forecasting',
  },
  {
    href: '/monitoring',
    label: 'Monitoring',
    description: 'Accuracy, drift, alerts',
    requires: [PERMISSIONS.viewMonitoring],
    group: 'operations',
  },
  {
    href: '/models',
    label: 'Model Registry',
    description: 'Versions and promotion',
    requires: [PERMISSIONS.viewModelRegistry],
    group: 'operations',
  },
  {
    href: '/admin/users',
    label: 'Users',
    description: 'Accounts and roles',
    requires: [PERMISSIONS.manageUsers],
    group: 'administration',
  },
  {
    href: '/admin/audit-logs',
    label: 'Audit Log',
    description: 'Who did what',
    requires: [PERMISSIONS.viewAuditLogs],
    group: 'administration',
  },
  {
    href: '/settings',
    label: 'Settings',
    description: 'Profile and password',
    // Everyone who can sign in can reach their own settings.
    requires: [],
    group: 'administration',
  },
];

/** Navigation entries a user may see. */
export function visibleNavEntries(
  user: PermissionHolder | null | undefined,
): NavEntry[] {
  if (!user) return [];
  return NAV_ENTRIES.filter(
    (entry) => entry.requires.length === 0 || canAny(user, ...entry.requires),
  );
}

/** Group labels, in display order. */
export const NAV_GROUPS: readonly {
  readonly id: NavEntry['group'];
  readonly label: string;
}[] = [
  { id: 'forecasting', label: 'Forecasting' },
  { id: 'operations', label: 'Operations' },
  { id: 'administration', label: 'Account' },
];
