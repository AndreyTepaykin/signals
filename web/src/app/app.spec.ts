import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { of } from 'rxjs';
import { ExpectancyPanelComponent } from './components/expectancy-panel.component';
import { ReliabilityDiagramComponent } from './components/reliability-diagram.component';
import { SignalCardComponent } from './components/signal-card.component';
import { StandDownBannerComponent } from './components/stand-down-banner.component';
import type { SignalCard } from './models/api.types';
import { LiveService, type LiveEvent } from './services/live.service';
import { pageRange } from './utils/pagination';

describe('SignalCardComponent', () => {
  let fixture: ComponentFixture<SignalCardComponent>;

  const card: SignalCard = {
    id: 1,
    action: 'long',
    status: 'alert',
    status_reason: null,
    coin: 'BTCUSDT',
    horizon: 'swing',
    decision_time: '',
    bar_close: '',
    source: 'live',
    locked_book: false,
    entry_price: null,
    stop_price: 1,
    pre_options_stop_price: 1,
    upper_barrier_price: null,
    lower_barrier_price: null,
    target_cap_price: null,
    one_r: null,
    vertical_deadline: null,
    edge_hidden: true,
    edge_hidden_reason: 'pool has 5 of 30 independent outcomes',
    estimates: {
      p_upper_before_stop: null,
      expected_r: null,
      expected_mae_r: null,
      median_hours_to_resolution: null,
      pool_level: null,
      pool_n: 5,
      expected_r_ci_low: null,
      expected_r_ci_high: null,
      pool_interval_confidence: null,
    },
    regime: {},
    options_line: { verdict: 'align', dealer_gamma_sign: 1 },
    trigger: 'squeeze',
    setup_family: 'squeeze',
    location: {},
    drivers: [],
    cost_line: { round_trip_bps: 12, taker_fee_bps_per_side: 4, slippage_bps_per_side: 2, funding_carry_bps_horizon: null },
    feature_hash: 'f',
    rule_hash: 'r',
    code_version: 'v',
    on_current_rule_hash: true,
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SignalCardComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(SignalCardComponent);
    fixture.componentRef.setInput('card', card);
    fixture.detectChanges();
  });

  it('hides estimates when edge_hidden', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Edge hidden');
    expect(el.textContent).not.toContain('E[R] 0.');
  });

  it('options line has no probability field', () => {
    expect('probability' in card.options_line).toBe(false);
  });

  it('gamma sign null renders no gamma text', () => {
    fixture.componentRef.setInput('card', {
      ...card,
      edge_hidden: false,
      options_line: { verdict: 'align' },
    });
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).not.toMatch(/γ/);
    expect(text.toLowerCase()).not.toContain('dealers short gamma');
    expect(text.toLowerCase()).not.toContain('dealers long gamma');
  });

  it('pre-fill card shows awaiting fill', () => {
    fixture.componentRef.setInput('card', {
      ...card,
      edge_hidden: false,
      entry_price: null,
      one_r: null,
      upper_barrier_price: null,
      lower_barrier_price: null,
      vertical_deadline: null,
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('awaiting fill');
  });
});

describe('ReliabilityDiagramComponent', () => {
  it('renders only provided bins', () => {
    const fixture = TestBed.createComponent(ReliabilityDiagramComponent);
    fixture.componentRef.setInput('bins', [
      { low: 0, high: 0.1, n_independent: 25, n_raw: 30, mean_probability: 0.05, observed_rate: 0.04, mean_stored_r: 0.1 },
    ]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('circle').length).toBe(1);
  });
});

describe('pageRange', () => {
  it('computes tracker pagination labels', () => {
    expect(pageRange(0, 50, 120)).toEqual({ from: 1, to: 50 });
    expect(pageRange(50, 50, 120)).toEqual({ from: 51, to: 100 });
    expect(pageRange(100, 50, 120)).toEqual({ from: 101, to: 120 });
  });
});

describe('ExpectancyPanelComponent', () => {
  it('renders insufficient-sample badge', async () => {
    await TestBed.configureTestingModule({ imports: [ExpectancyPanelComponent] }).compileComponents();
    const fixture = TestBed.createComponent(ExpectancyPanelComponent);
    fixture.componentRef.setInput('cohorts', [
      {
        label: 'replay/no_options_book/full',
        expectancy: {
          cohort: 'replay/no_options_book/full',
          flags: [],
          alert: { overall: {} as never, by_horizon: {} },
          shadow: { overall: {} as never, by_horizon: {} },
        },
        baselines: {
          cohort: 'replay/no_options_book/full',
          flags: [],
          comparisons: [
            {
              scope: 'all',
              app: {
                cohort: 'x',
                n_labels_raw: 1,
                n_labels_independent: 1,
                n_filled_raw: 1,
                n_filled_independent: 5,
                insufficient_sample: true,
                sample_note: 'too small',
                expectancy_r: 0,
                interval_low: -1,
                interval_high: 1,
                interval_confidence: 0.9,
                brier: null,
                n_with_probability: 0,
                brier_note: '',
              },
              flat_expectancy_r: 0,
              vs_flat: 'flat',
              baseline: { key: 'k', summary: null, expectancy_r: null },
              vs_swing_break: 'x',
              vs_candidates_subset: 'x',
            },
          ],
          kill_rule: null,
          kill_rule_preview: null,
          would_abstain: { alerts: 0, would_abstain: 0, alerts_independent: 0, would_abstain_independent: 0 },
        },
      },
    ]);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('insufficient sample');
  });
});

describe('StandDownBannerComponent', () => {
  it('lists feed breaches below banner', async () => {
    await TestBed.configureTestingModule({ imports: [StandDownBannerComponent] }).compileComponents();
    const fixture = TestBed.createComponent(StandDownBannerComponent);
    fixture.componentRef.setInput('state', {
      active: true,
      kinds: ['feed_stale'],
      feed_breaches: [{ feed: 'binance_kline', coin: 'BTCUSDT', reason: 'stale', staleness_seconds: 10, max_staleness_seconds: 100 }],
      active_event_windows: [],
      upcoming_event_windows: [],
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('binance_kline');
    expect(fixture.nativeElement.textContent).toContain('BTCUSDT');
  });
});

describe('LiveService WS mapping', () => {
  it('emits candidates vs labels separately', () => {
    const svc = new LiveService();
    const seen: LiveEvent[] = [];
    svc.onEvent((e) => seen.push(e));
    svc.dispatchForTest({ type: 'candidates' });
    svc.dispatchForTest({ type: 'labels' });
    expect(seen.map((s) => s.type)).toEqual(['candidates', 'labels']);
  });
});
