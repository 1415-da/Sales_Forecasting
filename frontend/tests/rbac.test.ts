/**
 * The frontend's RBAC mirror and the navigation it drives.
 *
 * These assert presentation, not security: the API re-checks every request.
 * What is being protected here is that a user is not shown a control that can
 * only ever return 403, and — more importantly — that the UI reads the
 * server's permission list rather than keeping its own copy of the grants.
 */
import { describe, expect, it } from 'vitest';

import {
  NAV_ENTRIES,
  NO_PERMISSION_MESSAGE,
  PERMISSIONS,
  ROLES,
  ROLE_DESCRIPTIONS,
  ROLE_LABELS,
  can,
  canAll,
  canAny,
  visibleNavEntries,
  type Permission,
  type Role,
} from '@/lib/rbac';

/** A user carrying exactly the listed permissions. */
const withPermissions = (...permissions: Permission[]) => ({ permissions });

/** The grants the API reports per role, as `/auth/me` would return them. */
const SERVER_GRANTS: Record<Role, Permission[]> = {
  VIEWER: [
    PERMISSIONS.viewForecast,
    PERMISSIONS.viewScenario,
    PERMISSIONS.viewModelPerformance,
    PERMISSIONS.viewModelRegistry,
    PERMISSIONS.viewMonitoring,
  ],
  BUSINESS_USER: [
    PERMISSIONS.viewForecast,
    PERMISSIONS.viewScenario,
    PERMISSIONS.viewModelPerformance,
    PERMISSIONS.viewModelRegistry,
    PERMISSIONS.viewMonitoring,
    PERMISSIONS.generateForecast,
    PERMISSIONS.exportForecast,
    PERMISSIONS.runScenario,
  ],
  ANALYST: [
    PERMISSIONS.viewForecast,
    PERMISSIONS.viewScenario,
    PERMISSIONS.viewModelPerformance,
    PERMISSIONS.viewModelRegistry,
    PERMISSIONS.viewMonitoring,
    PERMISSIONS.generateForecast,
    PERMISSIONS.exportForecast,
    PERMISSIONS.runScenario,
    PERMISSIONS.viewModelRegistryDetail,
    PERMISSIONS.viewMonitoringDetail,
  ],
  ML_ENGINEER: [
    PERMISSIONS.viewForecast,
    PERMISSIONS.viewScenario,
    PERMISSIONS.viewModelPerformance,
    PERMISSIONS.viewModelRegistry,
    PERMISSIONS.viewMonitoring,
    PERMISSIONS.generateForecast,
    PERMISSIONS.exportForecast,
    PERMISSIONS.runScenario,
    PERMISSIONS.viewModelRegistryDetail,
    PERMISSIONS.viewMonitoringDetail,
    PERMISSIONS.promoteModel,
    PERMISSIONS.archiveModel,
    PERMISSIONS.rollbackModel,
    PERMISSIONS.triggerRetraining,
    PERMISSIONS.manageAlerts,
    PERMISSIONS.viewAuditLogs,
  ],
  ADMIN: Object.values(PERMISSIONS),
};

describe('can', () => {
  it('reads the server-supplied list, not a local role table', () => {
    // A user whose role says ADMIN but whose permission list is empty holds
    // nothing. The list is the authority, so a stale local table cannot grant
    // access the server would refuse.
    const impostor = { role: 'ADMIN', permissions: [] as string[] };
    expect(can(impostor, PERMISSIONS.manageUsers)).toBe(false);
  });

  it('denies everything for an absent user', () => {
    expect(can(null, PERMISSIONS.viewForecast)).toBe(false);
    expect(can(undefined, PERMISSIONS.viewForecast)).toBe(false);
  });

  it('requires every permission for canAll and any for canAny', () => {
    const user = withPermissions(PERMISSIONS.viewForecast, PERMISSIONS.viewMonitoring);
    expect(canAll(user, PERMISSIONS.viewForecast, PERMISSIONS.viewMonitoring)).toBe(true);
    expect(canAll(user, PERMISSIONS.viewForecast, PERMISSIONS.manageUsers)).toBe(false);
    expect(canAny(user, PERMISSIONS.manageUsers, PERMISSIONS.viewForecast)).toBe(true);
    expect(canAny(user, PERMISSIONS.manageUsers, PERMISSIONS.promoteModel)).toBe(false);
  });
});

describe('navigation', () => {
  it('shows nothing to an unauthenticated visitor', () => {
    // The sign-in page renders without a shell; an empty sidebar would be
    // worse than none.
    expect(visibleNavEntries(null)).toEqual([]);
  });

  it.each(ROLES)('gives %s only the destinations its grants allow', (role) => {
    const entries = visibleNavEntries(withPermissions(...SERVER_GRANTS[role]));
    for (const entry of entries) {
      if (entry.requires.length === 0) continue;
      expect(
        canAny(withPermissions(...SERVER_GRANTS[role]), ...entry.requires),
      ).toBe(true);
    }
  });

  it('hides user management and the audit log from everyone but their roles', () => {
    const analyst = withPermissions(...SERVER_GRANTS.ANALYST);
    const engineer = withPermissions(...SERVER_GRANTS.ML_ENGINEER);
    const admin = withPermissions(...SERVER_GRANTS.ADMIN);

    const hrefs = (user: { permissions: string[] }) =>
      visibleNavEntries(user).map((entry) => entry.href);

    expect(hrefs(analyst)).not.toContain('/admin/users');
    expect(hrefs(analyst)).not.toContain('/admin/audit-logs');
    expect(hrefs(engineer)).not.toContain('/admin/users');
    // An ML engineer does see the audit log: model history is their concern.
    expect(hrefs(engineer)).toContain('/admin/audit-logs');
    expect(hrefs(admin)).toContain('/admin/users');
    expect(hrefs(admin)).toContain('/admin/audit-logs');
  });

  it('lets every signed-in role reach its own settings', () => {
    for (const role of ROLES) {
      const hrefs = visibleNavEntries(
        withPermissions(...SERVER_GRANTS[role]),
      ).map((entry) => entry.href);
      expect(hrefs).toContain('/settings');
    }
  });

  it('declares a requirement for every destination except settings', () => {
    // A page added to the nav without declaring what it needs would be
    // visible to everyone, including roles the API would refuse.
    for (const entry of NAV_ENTRIES) {
      if (entry.href === '/settings') continue;
      expect(entry.requires.length).toBeGreaterThan(0);
    }
  });

  it('has a label and description for every role', () => {
    for (const role of ROLES) {
      expect(ROLE_LABELS[role]).toBeTruthy();
      expect(ROLE_DESCRIPTIONS[role]).toBeTruthy();
    }
  });
});

describe('permission names', () => {
  it('match the API vocabulary exactly', () => {
    // These strings are compared against what `/auth/me` returns, so a typo
    // here silently disables a control rather than failing loudly.
    expect(Object.values(PERMISSIONS)).toEqual(
      expect.arrayContaining([
        'view_forecast',
        'generate_forecast',
        'export_forecast',
        'run_scenario',
        'view_scenario',
        'view_model_performance',
        'view_model_registry',
        'view_model_registry_detail',
        'promote_model',
        'archive_model',
        'rollback_model',
        'trigger_retraining',
        'view_monitoring',
        'view_monitoring_detail',
        'manage_alerts',
        'manage_users',
        'view_audit_logs',
        'view_all_audit_logs',
      ]),
    );
  });

  it('are all unique', () => {
    const values = Object.values(PERMISSIONS);
    expect(new Set(values).size).toBe(values.length);
  });
});

describe('the denial message', () => {
  it('is the exact wording the specification requires', () => {
    expect(NO_PERMISSION_MESSAGE).toBe(
      'You do not have permission to perform this action.',
    );
  });
});
