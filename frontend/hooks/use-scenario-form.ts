'use client';

/**
 * Scenario setup form state.
 *
 * The state machine is a pure reducer with pure selectors, and the hook is a
 * thin `useReducer` wrapper over it. That keeps the rules that matter -- when a
 * result is cleared, when it goes stale, when an idempotency key may be reused
 * -- testable without rendering anything.
 */

import { useCallback, useMemo, useReducer } from 'react';

import { generateIdempotencyKey } from '@/lib/api/scenario-api';
import {
  SCENARIO_LIMITS,
  type HistoryPoint,
  type PromotionIntensity,
  type ScenarioAssumptions,
  type ScenarioRequestInput,
  type ScenarioType,
} from '@/lib/types/scenario';

/* -------------------------------------------------------------------------- */
/* Options                                                                     */
/* -------------------------------------------------------------------------- */

export const HORIZON_OPTIONS = [7, 14, 30, 60, 90] as const;
export type HorizonDays = (typeof HORIZON_OPTIONS)[number];

export const CONFIDENCE_OPTIONS = [80, 95] as const;
export type ConfidenceLevel = (typeof CONFIDENCE_OPTIONS)[number];

/**
 * An 80% band is the 10th-90th percentile; 95% is the 2.5th-97.5th.
 *
 * Typed as a non-empty tuple to satisfy the request schema, which rejects an
 * empty `confidence_levels` array.
 */
export const CONFIDENCE_QUANTILES: Record<ConfidenceLevel, [number, ...number[]]> = {
  80: [0.1, 0.5, 0.9],
  95: [0.025, 0.5, 0.975],
};

export const IMPACT_LEVELS: PromotionIntensity[] = ['none', 'low', 'medium', 'high'];

/** Traffic slider bounds, in percent. Maps into the API's 0.5-2.0 multiplier. */
export const TRAFFIC_PCT_MIN = -30;
export const TRAFFIC_PCT_MAX = 50;

/** Promotion traffic-lift bounds, in percent. */
export const TRAFFIC_LIFT_MIN = 0;
export const TRAFFIC_LIFT_MAX = 100;

export const MAX_PROMOTION_DURATION_DAYS = 90;

/** Holidays offered in the picker. Labels are display-only. */
export interface HolidayOption {
  id: string;
  label: string;
  date: string;
}

export const HOLIDAY_OPTIONS: HolidayOption[] = [
  { id: 'new_years_day', label: "New Year's Day", date: '2026-01-01' },
  { id: 'presidents_day', label: "Presidents' Day", date: '2026-02-16' },
  { id: 'memorial_day', label: 'Memorial Day', date: '2026-05-25' },
  { id: 'independence_day', label: 'Independence Day', date: '2026-07-04' },
  { id: 'labor_day', label: 'Labor Day', date: '2026-09-07' },
  { id: 'thanksgiving', label: 'Thanksgiving', date: '2026-11-26' },
  { id: 'black_friday', label: 'Black Friday', date: '2026-11-27' },
  { id: 'christmas_day', label: 'Christmas Day', date: '2026-12-25' },
];

/* -------------------------------------------------------------------------- */
/* State                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Assumption inputs, held as the form sees them.
 *
 * Numeric fields are numbers (the controls are sliders and steppers, not free
 * text), while dates stay ISO strings so they round-trip to the API unchanged.
 */
export interface AssumptionsDraft {
  startDate: string;
  endDate: string;
  durationDays: number;
  intensity: PromotionIntensity;
  /** Discount as a percentage, 0-100. The API wants a 0-1 fraction. */
  discountPct: number;
  /** Expected traffic lift for a promotion, in percent. */
  trafficLiftPct: number;
  /** Traffic change for a traffic scenario, in percent (may be negative). */
  trafficChangePct: number;
  holidayId: string;
  customFeature: string;
  customValue: number;
}

/** A result is absent, current, or outdated by a later edit. */
export type ResultStatus = 'none' | 'fresh' | 'stale';

export interface ScenarioFormState {
  storeId: string;
  horizonDays: HorizonDays;
  modelVersion: string;
  confidenceLevel: ConfidenceLevel;
  scenarioType: ScenarioType;
  assumptions: AssumptionsDraft;
  touched: Record<string, boolean>;
  resultStatus: ResultStatus;
  /** Signature of the last submitted request, for idempotent retries. */
  idempotency: { signature: string; key: string } | null;
}

export const DEFAULT_ASSUMPTIONS: AssumptionsDraft = {
  startDate: '',
  endDate: '',
  durationDays: 7,
  intensity: 'medium',
  discountPct: 20,
  trafficLiftPct: 10,
  trafficChangePct: 10,
  holidayId: '',
  customFeature: '',
  customValue: 0,
};

export interface ScenarioFormInit {
  storeId?: string;
  horizonDays?: HorizonDays;
  modelVersion?: string;
  confidenceLevel?: ConfidenceLevel;
  scenarioType?: ScenarioType;
  assumptions?: Partial<AssumptionsDraft>;
}

export function initialFormState(init: ScenarioFormInit = {}): ScenarioFormState {
  return {
    storeId: init.storeId ?? '',
    horizonDays: init.horizonDays ?? 30,
    modelVersion: init.modelVersion ?? SCENARIO_LIMITS.defaultModelVersion,
    confidenceLevel: init.confidenceLevel ?? 80,
    scenarioType: init.scenarioType ?? 'promotion',
    assumptions: { ...DEFAULT_ASSUMPTIONS, ...init.assumptions },
    touched: {},
    resultStatus: 'none',
    idempotency: null,
  };
}

/* -------------------------------------------------------------------------- */
/* Reducer                                                                     */
/* -------------------------------------------------------------------------- */

export type ScenarioFormAction =
  | { type: 'setStoreId'; value: string }
  | { type: 'setHorizonDays'; value: HorizonDays }
  | { type: 'setModelVersion'; value: string }
  | { type: 'setConfidenceLevel'; value: ConfidenceLevel }
  | { type: 'setScenarioType'; value: ScenarioType }
  | { type: 'setAssumption'; field: keyof AssumptionsDraft; value: string | number }
  | { type: 'touch'; field: string }
  | { type: 'runStarted'; signature: string; key: string }
  | { type: 'runSucceeded' }
  | { type: 'reset'; init?: ScenarioFormInit };

/**
 * Fields whose change invalidates the result outright.
 *
 * A different store, model, or horizon describes a different question, so the
 * previous answer is not merely stale -- it is about something else.
 */
const RESULT_CLEARING_FIELDS = ['storeId', 'modelVersion', 'horizonDays'] as const;

/** Mark the result stale, but only when one exists. */
function staleAfterEdit(state: ScenarioFormState): ResultStatus {
  return state.resultStatus === 'none' ? 'none' : 'stale';
}

export function scenarioFormReducer(
  state: ScenarioFormState,
  action: ScenarioFormAction,
): ScenarioFormState {
  switch (action.type) {
    case 'setStoreId':
      if (action.value === state.storeId) return state;
      return { ...state, storeId: action.value, resultStatus: 'none', idempotency: null };

    case 'setModelVersion':
      if (action.value === state.modelVersion) return state;
      return {
        ...state,
        modelVersion: action.value,
        resultStatus: 'none',
        idempotency: null,
      };

    case 'setHorizonDays':
      if (action.value === state.horizonDays) return state;
      return {
        ...state,
        horizonDays: action.value,
        resultStatus: 'none',
        idempotency: null,
      };

    case 'setConfidenceLevel':
      if (action.value === state.confidenceLevel) return state;
      // The confidence band changes the requested quantiles, so the existing
      // result no longer answers the question -- but the scenario itself is
      // unchanged, so this is a staleness, not a reset.
      return {
        ...state,
        confidenceLevel: action.value,
        resultStatus: staleAfterEdit(state),
      };

    case 'setScenarioType':
      if (action.value === state.scenarioType) return state;
      return {
        ...state,
        scenarioType: action.value,
        resultStatus: staleAfterEdit(state),
      };

    case 'setAssumption': {
      const current = state.assumptions[action.field];
      if (current === action.value) return state;
      return {
        ...state,
        assumptions: { ...state.assumptions, [action.field]: action.value },
        touched: { ...state.touched, [action.field]: true },
        resultStatus: staleAfterEdit(state),
      };
    }

    case 'touch':
      if (state.touched[action.field]) return state;
      return { ...state, touched: { ...state.touched, [action.field]: true } };

    case 'runStarted':
      return {
        ...state,
        idempotency: { signature: action.signature, key: action.key },
      };

    case 'runSucceeded':
      return { ...state, resultStatus: 'fresh' };

    case 'reset':
      return initialFormState(action.init);

    default:
      return state;
  }
}

/* -------------------------------------------------------------------------- */
/* Derivation                                                                  */
/* -------------------------------------------------------------------------- */

/** Assumption fields each scenario type actually uses. */
export const FIELDS_BY_SCENARIO_TYPE: Record<ScenarioType, (keyof AssumptionsDraft)[]> = {
  promotion: ['startDate', 'durationDays', 'intensity', 'discountPct', 'trafficLiftPct'],
  traffic: ['startDate', 'durationDays', 'trafficChangePct'],
  closure: ['startDate', 'endDate'],
  holiday: ['holidayId', 'intensity'],
  price: ['startDate', 'durationDays', 'discountPct'],
  custom: ['startDate', 'durationDays', 'customFeature', 'customValue'],
};

/** Add whole days to an ISO date, in UTC. */
export function addDays(isoDate: string, days: number): string {
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

/** Every date from `start` to `end` inclusive. */
export function expandDateRange(start: string, end: string): string[] {
  const from = new Date(`${start}T00:00:00Z`);
  const to = new Date(`${end}T00:00:00Z`);
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime()) || to < from) return [];

  const dates: string[] = [];
  for (let cursor = from; cursor <= to; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
    dates.push(cursor.toISOString().slice(0, 10));
  }
  return dates;
}

/** Percent change to the API's multiplier, clamped to its accepted range. */
export function pctToMultiplier(pct: number): number {
  const multiplier = 1 + pct / 100;
  return Math.min(2, Math.max(0.5, Number(multiplier.toFixed(4))));
}

/** Build the API's assumptions object from the draft. */
export function buildAssumptions(state: ScenarioFormState): ScenarioAssumptions {
  const { assumptions: draft, scenarioType } = state;
  const base: ScenarioAssumptions = {
    scenario_type: scenarioType,
    start_date: null,
    end_date: null,
    duration_days: null,
    promotion_intensity: 'none',
    discount_depth: null,
    traffic_multiplier: null,
    closure_dates: [],
    custom_overrides: {},
  };

  switch (scenarioType) {
    case 'promotion':
      return {
        ...base,
        start_date: draft.startDate || null,
        end_date: draft.startDate
          ? addDays(draft.startDate, Math.max(1, draft.durationDays) - 1)
          : null,
        promotion_intensity: draft.intensity,
        discount_depth: draft.discountPct / 100,
        // Sent for models that carry a traffic feature; the promotion handler
        // itself only reads the promotion levers.
        traffic_multiplier: draft.trafficLiftPct
          ? pctToMultiplier(draft.trafficLiftPct)
          : null,
      };

    case 'traffic':
      return {
        ...base,
        start_date: draft.startDate || null,
        end_date: draft.startDate
          ? addDays(draft.startDate, Math.max(1, draft.durationDays) - 1)
          : null,
        traffic_multiplier: pctToMultiplier(draft.trafficChangePct),
      };

    case 'closure':
      return {
        ...base,
        closure_dates:
          draft.startDate && draft.endDate
            ? expandDateRange(draft.startDate, draft.endDate)
            : [],
      };

    case 'holiday': {
      const holiday = HOLIDAY_OPTIONS.find((option) => option.id === draft.holidayId);
      return {
        ...base,
        start_date: holiday?.date ?? null,
        end_date: holiday?.date ?? null,
        promotion_intensity: draft.intensity,
      };
    }

    case 'price':
      return {
        ...base,
        start_date: draft.startDate || null,
        end_date: draft.startDate
          ? addDays(draft.startDate, Math.max(1, draft.durationDays) - 1)
          : null,
        discount_depth: draft.discountPct / 100,
      };

    case 'custom':
      return {
        ...base,
        start_date: draft.startDate || null,
        end_date: draft.startDate
          ? addDays(draft.startDate, Math.max(1, draft.durationDays) - 1)
          : null,
        custom_overrides: draft.customFeature
          ? { [draft.customFeature]: draft.customValue }
          : {},
      };

    default:
      return base;
  }
}

/** Assemble the full API request from form state plus supplied history. */
export function buildScenarioRequest(
  state: ScenarioFormState,
  history: HistoryPoint[],
): ScenarioRequestInput {
  return {
    store_id: state.storeId,
    model_version: state.modelVersion,
    horizon_days: state.horizonDays,
    confidence_levels: CONFIDENCE_QUANTILES[state.confidenceLevel],
    history,
    scenario: buildAssumptions(state),
  };
}

/**
 * Stable signature of a request, used to decide idempotency-key reuse.
 *
 * Key order is normalised, so two structurally identical requests produce the
 * same signature regardless of how they were built.
 */
export function requestSignature(request: ScenarioRequestInput): string {
  return JSON.stringify(request, (_key, value) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
          a.localeCompare(b),
        ),
      );
    }
    return value;
  });
}

/* -------------------------------------------------------------------------- */
/* Validation                                                                  */
/* -------------------------------------------------------------------------- */

export type FormErrors = Partial<Record<string, string>>;

export interface ValidationContext {
  /** History rows available for the selected store. */
  historyCount?: number;
  /** Whether the selected scenario type is supported by the chosen model. */
  scenarioSupported?: boolean;
  /** Features the model exposes, for the custom-override picker. */
  availableFeatures?: string[];
}

/**
 * Validate the form.
 *
 * Mirrors the API's own rules so the user sees a problem before paying for a
 * round trip; the API remains the authority.
 */
export function validateForm(
  state: ScenarioFormState,
  context: ValidationContext = {},
): FormErrors {
  const errors: FormErrors = {};
  const { assumptions: draft, scenarioType } = state;

  if (!state.storeId) errors.storeId = 'Select a store.';
  if (!HORIZON_OPTIONS.includes(state.horizonDays)) {
    errors.horizonDays = 'Choose a forecast horizon.';
  }
  if (!state.modelVersion) errors.modelVersion = 'Select a model version.';

  if (context.historyCount !== undefined && context.historyCount < SCENARIO_LIMITS.minHistoryRows) {
    errors.history = `At least ${SCENARIO_LIMITS.minHistoryRows} days of history are required.`;
  }
  if (context.scenarioSupported === false) {
    errors.scenarioType = 'This scenario is not supported by the selected model.';
  }

  const needs = (field: keyof AssumptionsDraft) =>
    FIELDS_BY_SCENARIO_TYPE[scenarioType].includes(field);

  if (needs('startDate') && !draft.startDate) {
    errors.startDate = 'Choose a start date.';
  }
  if (needs('durationDays')) {
    if (!Number.isFinite(draft.durationDays) || draft.durationDays < 1) {
      errors.durationDays = 'Duration must be at least 1 day.';
    } else if (draft.durationDays > MAX_PROMOTION_DURATION_DAYS) {
      errors.durationDays = `Duration cannot exceed ${MAX_PROMOTION_DURATION_DAYS} days.`;
    } else if (draft.durationDays > state.horizonDays) {
      errors.durationDays = 'Duration cannot exceed the forecast horizon.';
    }
  }
  if (needs('discountPct') && (draft.discountPct < 0 || draft.discountPct > 100)) {
    errors.discountPct = 'Discount must be between 0% and 100%.';
  }
  if (
    needs('trafficLiftPct') &&
    (draft.trafficLiftPct < TRAFFIC_LIFT_MIN || draft.trafficLiftPct > TRAFFIC_LIFT_MAX)
  ) {
    errors.trafficLiftPct = `Traffic lift must be between ${TRAFFIC_LIFT_MIN}% and ${TRAFFIC_LIFT_MAX}%.`;
  }
  if (
    needs('trafficChangePct') &&
    (draft.trafficChangePct < TRAFFIC_PCT_MIN || draft.trafficChangePct > TRAFFIC_PCT_MAX)
  ) {
    // The `+` is kept so the sign of each bound is unambiguous next to a
    // negative lower bound, matching the slider's own hint text.
    errors.trafficChangePct = `Traffic change must be between ${TRAFFIC_PCT_MIN}% and +${TRAFFIC_PCT_MAX}%.`;
  }

  if (scenarioType === 'closure') {
    if (!draft.endDate) {
      errors.endDate = 'Choose an end date.';
    } else if (draft.startDate && draft.endDate < draft.startDate) {
      errors.endDate = 'End date must be on or after the start date.';
    }
  }

  if (scenarioType === 'holiday' && !draft.holidayId) {
    errors.holidayId = 'Choose a holiday.';
  }

  if (scenarioType === 'custom') {
    if (!draft.customFeature) {
      errors.customFeature = 'Choose a feature to override.';
    } else if (
      context.availableFeatures &&
      !context.availableFeatures.includes(draft.customFeature)
    ) {
      errors.customFeature = 'The selected model does not include this feature.';
    }
    if (!Number.isFinite(draft.customValue)) {
      errors.customValue = 'Enter a numeric value.';
    }
  }

  return errors;
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                        */
/* -------------------------------------------------------------------------- */

export interface UseScenarioFormOptions extends ScenarioFormInit {
  history?: HistoryPoint[];
  /**
   * Whether a scenario type is supported by the selected model.
   *
   * A predicate rather than a boolean: support depends on the scenario type,
   * which lives in this hook's own state, so the caller cannot compute it
   * before calling the hook.
   */
  isScenarioTypeSupported?: (type: ScenarioType) => boolean;
  availableFeatures?: string[];
}

export interface UseScenarioFormResult {
  state: ScenarioFormState;
  errors: FormErrors;
  /** Errors for fields the user has interacted with, for inline display. */
  visibleErrors: FormErrors;
  isValid: boolean;
  resultStatus: ResultStatus;
  /** True once the result no longer reflects the current inputs. */
  isStale: boolean;
  request: ScenarioRequestInput;
  /** Assumption fields the selected scenario type uses. */
  activeFields: (keyof AssumptionsDraft)[];

  setStoreId: (value: string) => void;
  setHorizonDays: (value: HorizonDays) => void;
  setModelVersion: (value: string) => void;
  setConfidenceLevel: (value: ConfidenceLevel) => void;
  setScenarioType: (value: ScenarioType) => void;
  setAssumption: (field: keyof AssumptionsDraft, value: string | number) => void;
  touch: (field: string) => void;
  reset: (init?: ScenarioFormInit) => void;

  /**
   * Claim an idempotency key for a submission.
   *
   * Returns the existing key when the request is byte-identical to the last
   * submitted one, so a retry replays rather than recomputing; any edit yields
   * a new key.
   */
  claimIdempotencyKey: () => string;
  markRunSucceeded: () => void;
}

export function useScenarioForm(
  options: UseScenarioFormOptions = {},
): UseScenarioFormResult {
  const { history = [], isScenarioTypeSupported, availableFeatures, ...init } = options;
  const [state, dispatch] = useReducer(scenarioFormReducer, init, initialFormState);

  const request = useMemo(() => buildScenarioRequest(state, history), [state, history]);

  const errors = useMemo(
    () =>
      validateForm(state, {
        historyCount: history.length,
        scenarioSupported: isScenarioTypeSupported?.(state.scenarioType),
        availableFeatures,
      }),
    [state, history.length, isScenarioTypeSupported, availableFeatures],
  );

  const visibleErrors = useMemo(() => {
    const visible: FormErrors = {};
    for (const [field, message] of Object.entries(errors)) {
      // Always show blocking context errors; defer field errors until touched.
      if (
        state.touched[field] ||
        ['storeId', 'history', 'scenarioType', 'modelVersion', 'horizonDays'].includes(field)
      ) {
        visible[field] = message;
      }
    }
    return visible;
  }, [errors, state.touched]);

  const claimIdempotencyKey = useCallback(() => {
    const signature = requestSignature(request);
    if (state.idempotency?.signature === signature) {
      return state.idempotency.key;
    }
    const key = generateIdempotencyKey();
    dispatch({ type: 'runStarted', signature, key });
    return key;
  }, [request, state.idempotency]);

  return {
    state,
    errors,
    visibleErrors,
    isValid: Object.keys(errors).length === 0,
    resultStatus: state.resultStatus,
    isStale: state.resultStatus === 'stale',
    request,
    activeFields: FIELDS_BY_SCENARIO_TYPE[state.scenarioType],

    setStoreId: useCallback((value: string) => dispatch({ type: 'setStoreId', value }), []),
    setHorizonDays: useCallback(
      (value: HorizonDays) => dispatch({ type: 'setHorizonDays', value }),
      [],
    ),
    setModelVersion: useCallback(
      (value: string) => dispatch({ type: 'setModelVersion', value }),
      [],
    ),
    setConfidenceLevel: useCallback(
      (value: ConfidenceLevel) => dispatch({ type: 'setConfidenceLevel', value }),
      [],
    ),
    setScenarioType: useCallback(
      (value: ScenarioType) => dispatch({ type: 'setScenarioType', value }),
      [],
    ),
    setAssumption: useCallback(
      (field: keyof AssumptionsDraft, value: string | number) =>
        dispatch({ type: 'setAssumption', field, value }),
      [],
    ),
    touch: useCallback((field: string) => dispatch({ type: 'touch', field }), []),
    reset: useCallback((resetInit?: ScenarioFormInit) => {
      dispatch({ type: 'reset', init: resetInit });
    }, []),
    claimIdempotencyKey,
    markRunSucceeded: useCallback(() => dispatch({ type: 'runSucceeded' }), []),
  };
}
