export type Source = 'live' | 'replay';

export interface SignalCard {
  id: number;
  action: string;
  status: string;
  status_reason: string | null;
  coin: string;
  horizon: string;
  decision_time: string;
  bar_close: string;
  source: string;
  locked_book: boolean;
  entry_price: number | null;
  stop_price: number | null;
  pre_options_stop_price: number | null;
  upper_barrier_price: number | null;
  lower_barrier_price: number | null;
  target_cap_price: number | null;
  one_r: number | null;
  vertical_deadline: string | null;
  edge_hidden: boolean;
  edge_hidden_reason: string | null;
  estimates: {
    p_upper_before_stop: number | null;
    expected_r: number | null;
    expected_mae_r: number | null;
    median_hours_to_resolution: number | null;
    pool_level: string | null;
    pool_n: number | null;
    expected_r_ci_low: number | null;
    expected_r_ci_high: number | null;
    pool_interval_confidence: number | null;
  };
  regime: Record<string, string | number | null>;
  options_line: Record<string, string | number | null>;
  trigger: string;
  setup_family: string;
  location: Record<string, number | null>;
  drivers: { name: string; value: unknown }[];
  cost_line: Record<string, number | null>;
  feature_hash: string;
  rule_hash: string;
  code_version: string;
  on_current_rule_hash: boolean;
}

export interface CardListResponse {
  items: SignalCard[];
  total: number;
  limit: number;
  offset: number;
}

export interface TrackerOptionsSnapshot {
  verdict: string | null;
  venue: string | null;
  dealer_gamma_sign: number | null;
  nearest_path_strike: number | null;
  hours_to_expiry: number | null;
  expression: string | null;
  gamma_flip_distance_atr: number | null;
  nearest_path_strike_distance_atr: number | null;
  skew_25d: number | null;
  iv_minus_rv: number | null;
}

export interface TrackerRow {
  candidate_id: number;
  coin: string;
  horizon: string;
  direction: string;
  status: string;
  decision_time: string;
  fill_time: string;
  resolved_at: string;
  feature_hash: string;
  rule_hash: string;
  code_version: string;
  first_barrier: string;
  stored_r: number | null;
  price_r: number | null;
  fees_r: number | null;
  funding_r: number | null;
  mae_r: number | null;
  mfe_r: number | null;
  hours_to_mfe: number | null;
  hours_to_resolution: number | null;
  stop_price: number | null;
  upper_barrier_price: number | null;
  target_cap_price: number | null;
  barrier_ambiguous: boolean;
  gapped_stop: boolean;
  funding_partial: boolean;
  no_entry: boolean;
  options_snapshot: TrackerOptionsSnapshot | null;
  regime_trend_family: string | null;
  regime_vol_pct: number | null;
  regime_session: string | null;
  regime_event_flag: boolean | null;
  regime_is_weekend: boolean | null;
  expiry_during_hold: boolean | null;
}

export interface TrackerListResponse {
  items: TrackerRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface FeedHealthRow {
  feed: string;
  coin: string | null;
  status: string;
  last_print_at: string | null;
  last_available_at: string | null;
  last_checked_at: string | null;
  staleness_seconds: number | null;
  max_staleness_seconds: number | null;
  detail: string | null;
}

export interface FeedHealthResponse {
  rows: FeedHealthRow[];
  stand_down_breaches: Record<string, unknown>[];
}

export interface StandDownEventWindow {
  kind: string;
  coin: string | null;
  event_time: string;
  window_start: string;
  window_end: string;
  title: string | null;
}

export interface FeedBreach {
  feed: string;
  coin: string | null;
  reason: string;
  staleness_seconds: number | null;
  max_staleness_seconds: number | null;
}

export interface StandDownState {
  active: boolean;
  kinds: string[];
  feed_breaches: FeedBreach[];
  active_event_windows: StandDownEventWindow[];
  upcoming_event_windows: StandDownEventWindow[];
}

export interface CellSummary {
  cohort: string;
  n_labels_raw: number;
  n_labels_independent: number;
  n_filled_raw: number;
  n_filled_independent: number;
  insufficient_sample: boolean;
  sample_note: string;
  expectancy_r: number | null;
  interval_low: number | null;
  interval_high: number | null;
  interval_confidence: number;
  brier: number | null;
  n_with_probability: number;
  brier_note: string;
}

export interface ExpectancyCohort {
  cohort: string;
  flags: string[];
  alert: { overall: CellSummary; by_horizon: Record<string, CellSummary> };
  shadow: { overall: CellSummary; by_horizon: Record<string, CellSummary> };
}

export interface ExpectancyResponse {
  rule_hash: string | null;
  built_at: string;
  cohorts: ExpectancyCohort[];
}

export interface NaiveBaseline {
  key: string;
  summary: CellSummary | null;
  expectancy_r: number | null;
}

export interface Comparison {
  scope: string;
  app: CellSummary;
  flat_expectancy_r: number;
  vs_flat: string;
  baseline: NaiveBaseline;
  vs_swing_break: string;
  vs_candidates_subset: string;
}

export interface KillRuleResult {
  status: string;
  n_independent: number;
  floor: number;
  app_interval: [number, number] | null;
  baseline_expectancy_r: number | null;
  note: string;
  binding: boolean;
}

export interface WouldAbstain {
  alerts: number;
  would_abstain: number;
  alerts_independent: number;
  would_abstain_independent: number;
}

export interface BaselinesCohort {
  cohort: string;
  comparisons: Comparison[];
  kill_rule: KillRuleResult | null;
  kill_rule_preview: KillRuleResult | null;
  would_abstain: WouldAbstain;
  flags: string[];
}

export interface BaselinesResponse {
  rule_hash: string | null;
  built_at: string;
  cohorts: BaselinesCohort[];
}

export interface ReliabilityBin {
  low: number;
  high: number;
  n_raw: number;
  n_independent: number;
  mean_probability: number | null;
  observed_rate: number | null;
  mean_stored_r: number | null;
}

export interface ReliabilityDataset {
  cohort: string;
  note: string;
  bins: ReliabilityBin[];
  n_independent_with_probability: number;
  available: boolean;
}

export interface CalibrationCohort {
  cohort: string;
  reliability: ReliabilityDataset;
  reliability_all_statuses: ReliabilityDataset;
  brier: number | null;
  brier_note: string;
}

export interface CalibrationResponse {
  built_at: string;
  cohorts: CalibrationCohort[];
}
