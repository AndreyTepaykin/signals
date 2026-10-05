import { Component, input } from '@angular/core';
import type { SignalCard } from '../models/api.types';
import { fmtNum, fmtPct, fmtPrice, fmtR, fmtUtc, hashPrefix } from '../utils/format';

@Component({
  selector: 'app-signal-card',
  standalone: true,
  template: `
    <article class="card">
      <header class="head">
        <span class="coin">{{ card().coin }}</span>
        <span class="action" [class]="actionClass()">{{ actionLabel() }}</span>
        <span class="hzn">{{ card().horizon }}</span>
        <span class="status">{{ card().status }}</span>
        @if (card().status_reason) {
          <span class="reason">{{ card().status_reason }}</span>
        }
        @if (card().locked_book) {
          <span class="badge">locked book</span>
        }
        <span class="times mono">
          decision {{ fmtUtc(card().decision_time) }} · bar close {{ fmtUtc(card().bar_close) }} · {{ card().source }}
        </span>
      </header>

      <section class="block">
        <h4>Prices</h4>
        <dl class="grid">
          <dt>Entry</dt>
          <dd class="mono">{{ fillPrice(card().entry_price) }}</dd>
          <dt>Stop</dt>
          <dd class="mono">{{ fmtPrice(card().stop_price) }}</dd>
          @if (preOptionsDiffers()) {
            <dt>Pre-options stop</dt>
            <dd class="mono">{{ fmtPrice(card().pre_options_stop_price) }}</dd>
          }
          <dt>Upper barrier</dt>
          <dd class="mono">{{ fillPrice(card().upper_barrier_price) }}</dd>
          <dt>Lower barrier</dt>
          <dd class="mono">{{ fillPrice(card().lower_barrier_price) }}</dd>
          <dt>Target</dt>
          <dd class="mono">
            @if (card().target_cap_price != null) {
              {{ fmtPrice(card().target_cap_price) }} <em class="cap-note">capped</em>
            } @else {
              —
            }
          </dd>
          <dt>1R</dt>
          <dd class="mono">{{ fillPrice(card().one_r) }}</dd>
          <dt>Vertical deadline</dt>
          <dd class="mono">{{ fillTime(card().vertical_deadline) }}</dd>
        </dl>
      </section>

      <section class="block">
        <h4>Estimates</h4>
        @if (card().edge_hidden) {
          <p class="warn">Edge hidden: {{ card().edge_hidden_reason }}</p>
        } @else {
          <dl class="grid">
            <dt>P(upper before stop)</dt>
            <dd class="mono">{{ fmtPct(card().estimates.p_upper_before_stop) }}</dd>
            <dt>Expected R</dt>
            <dd class="mono">
              {{ fmtR(card().estimates.expected_r) }}
              [{{ fmtR(card().estimates.expected_r_ci_low) }}, {{ fmtR(card().estimates.expected_r_ci_high) }}]
              @if (card().estimates.pool_interval_confidence != null) {
                ({{ fmtPct(card().estimates.pool_interval_confidence, 0) }} interval conf)
              }
            </dd>
            <dt>Expected MAE</dt>
            <dd class="mono">{{ fmtR(card().estimates.expected_mae_r) }} R</dd>
            <dt>Median hours</dt>
            <dd class="mono">{{ fmtR(card().estimates.median_hours_to_resolution) }}</dd>
            <dt>Pool</dt>
            <dd>{{ card().estimates.pool_level ?? '—' }} (n={{ card().estimates.pool_n ?? '—' }})</dd>
          </dl>
        }
      </section>

      <section class="block">
        <h4>Regime</h4>
        <p>{{ regimeLine() }}</p>
      </section>

      @if (hasOptionsLine()) {
        <section class="block">
          <h4>Options</h4>
          <dl class="grid">
            @if (card().options_line['verdict'] != null) {
              <dt>Verdict</dt>
              <dd>{{ card().options_line['verdict'] }}</dd>
            }
            @if (card().options_line['venue'] != null) {
              <dt>Venue</dt>
              <dd>{{ card().options_line['venue'] }}</dd>
            }
            @if (card().options_line['vol_tag'] != null) {
              <dt>Vol</dt>
              <dd>{{ card().options_line['vol_tag'] }}</dd>
            }
            @if (gammaLabel()) {
              <dt>Gamma</dt>
              <dd>{{ gammaLabel() }}</dd>
            }
            @if (card().options_line['nearest_path_strike'] != null) {
              <dt>Nearest path strike</dt>
              <dd class="mono">{{ fmtPrice(numOpt('nearest_path_strike')) }}</dd>
            }
            @if (card().options_line['hours_to_expiry'] != null) {
              <dt>Hours to expiry</dt>
              <dd class="mono">{{ fmtR(numOpt('hours_to_expiry')) }} h</dd>
            }
            @if (card().options_line['expression'] != null) {
              <dt>Expression</dt>
              <dd>{{ card().options_line['expression'] }}</dd>
            }
            @if (card().options_line['size_multiplier'] != null) {
              <dt>Size mult</dt>
              <dd class="mono">×{{ fmtNum(card().options_line['size_multiplier']) }}</dd>
            }
            @if (card().options_line['influence'] != null) {
              <dt>Influence</dt>
              <dd class="mono">{{ fmtNum(card().options_line['influence']) }}</dd>
            }
            @if (card().options_line['confidence'] != null) {
              <dt>Confidence</dt>
              <dd class="mono">{{ fmtPct(numOpt('confidence')) }}</dd>
            }
          </dl>
        </section>
      }

      <section class="block">
        <h4>Trigger &amp; location</h4>
        <p>{{ card().trigger }} · {{ card().setup_family }}</p>
        @if (hasLocation()) {
          <dl class="grid">
            @if (card().location['vwap_distance_atr'] != null) {
              <dt>VWAP dist</dt>
              <dd class="mono">{{ fmtR(card().location['vwap_distance_atr']) }} ATR</dd>
            }
            @if (card().location['value_area_high_distance_atr'] != null) {
              <dt>VA high dist</dt>
              <dd class="mono">{{ fmtR(card().location['value_area_high_distance_atr']) }} ATR</dd>
            }
            @if (card().location['value_area_low_distance_atr'] != null) {
              <dt>VA low dist</dt>
              <dd class="mono">{{ fmtR(card().location['value_area_low_distance_atr']) }} ATR</dd>
            }
            @if (card().location['chandelier_stop_distance_atr'] != null) {
              <dt>Chandelier dist</dt>
              <dd class="mono">{{ fmtR(card().location['chandelier_stop_distance_atr']) }} ATR</dd>
            }
          </dl>
        }
      </section>

      @if (card().drivers.length) {
        <section class="block">
          <h4>Drivers</h4>
          <div class="chips">
            @for (d of card().drivers; track d.name) {
              <span class="chip mono">{{ d.name }} {{ driverValue(d.value) }}</span>
            }
          </div>
        </section>
      }

      <section class="block cost mono">
        taker {{ fmtNum(card().cost_line['taker_fee_bps_per_side'], 0) }} bps/side, slippage
        {{ fmtNum(card().cost_line['slippage_bps_per_side'], 0) }} bps/side, round trip
        {{ fmtNum(card().cost_line['round_trip_bps'], 0) }} bps, funding band
        @if (card().cost_line['funding_carry_bps_horizon'] != null) {
          {{ fmtNum(card().cost_line['funding_carry_bps_horizon']) }} bps
        } @else {
          n/a
        }
      </section>

      <footer class="foot mono">
        f {{ hashPrefix(card().feature_hash) }} · r {{ hashPrefix(card().rule_hash) }} · {{ card().code_version }}
        @if (!card().on_current_rule_hash) {
          <span class="warn">old rule hash</span>
        }
      </footer>
    </article>
  `,
  styles: [
    `
      .card {
        border: 1px solid #ccc;
        padding: 0.75rem;
        margin-bottom: 0.75rem;
        font-size: 0.9rem;
      }
      .head {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem 0.6rem;
        align-items: baseline;
        margin-bottom: 0.5rem;
      }
      .coin {
        font-weight: 700;
      }
      .action {
        font-weight: 700;
        letter-spacing: 0.03em;
      }
      .action.long {
        color: #060;
      }
      .action.short {
        color: #900;
      }
      .action.watch {
        color: #555;
      }
      .badge {
        background: #eef;
        padding: 0 0.35rem;
        font-size: 0.75rem;
        border-radius: 3px;
      }
      .times {
        flex-basis: 100%;
        font-size: 0.8rem;
        color: #444;
      }
      .block {
        margin: 0.5rem 0;
      }
      .block h4 {
        margin: 0 0 0.25rem;
        font-size: 0.75rem;
        text-transform: uppercase;
        color: #666;
      }
      .grid {
        display: grid;
        grid-template-columns: auto 1fr;
        gap: 0.15rem 0.75rem;
        margin: 0;
      }
      dt {
        color: #555;
      }
      dd {
        margin: 0;
      }
      .mono {
        font-family: ui-monospace, monospace;
      }
      .warn {
        color: #a00;
        font-weight: 600;
      }
      .cap-note {
        color: #633;
        font-style: normal;
        font-size: 0.85em;
      }
      .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
      }
      .chip {
        background: #f3f3f3;
        padding: 0.15rem 0.4rem;
        border-radius: 4px;
        font-size: 0.8rem;
      }
      .cost {
        font-size: 0.85rem;
        color: #333;
      }
      .foot {
        margin-top: 0.5rem;
        padding-top: 0.35rem;
        border-top: 1px solid #eee;
        font-size: 0.8rem;
        color: #555;
      }
      .reason {
        font-size: 0.85rem;
        color: #633;
      }
    `,
  ],
})
export class SignalCardComponent {
  readonly card = input.required<SignalCard>();

  readonly fmtR = fmtR;
  readonly fmtPrice = fmtPrice;
  readonly fmtUtc = fmtUtc;
  readonly fmtNum = fmtNum;
  readonly fmtPct = fmtPct;
  readonly hashPrefix = hashPrefix;

  awaitingFill(): boolean {
    return this.card().entry_price == null;
  }

  fillPrice(v: number | null): string {
    return this.awaitingFill() ? 'awaiting fill' : fmtPrice(v);
  }

  fillTime(iso: string | null): string {
    return this.awaitingFill() || iso == null ? 'awaiting fill' : fmtUtc(iso);
  }

  actionLabel(): string {
    const a = this.card().action.toLowerCase();
    if (a === 'long' || a === 'short') {
      return a.toUpperCase();
    }
    return 'WATCH';
  }

  actionClass(): string {
    const a = this.card().action.toLowerCase();
    if (a === 'short') {
      return 'short';
    }
    if (a === 'long') {
      return 'long';
    }
    return 'watch';
  }

  preOptionsDiffers(): boolean {
    const c = this.card();
    if (c.pre_options_stop_price == null || c.stop_price == null) {
      return false;
    }
    return c.pre_options_stop_price !== c.stop_price;
  }

  regimeLine(): string {
    const r = this.card().regime;
    const trend = r['trend_family'] ?? '—';
    const bucket = r['vol_bucket'] ?? '—';
    const pct = r['vol_percentile'];
    const vol =
      pct != null && typeof pct === 'number' ? `${bucket} (${fmtPct(pct)})` : String(bucket);
    const session = r['session'] ?? '—';
    return `${trend} × ${vol} × ${session}`;
  }

  gammaLabel(): string | null {
    const sign = this.card().options_line['dealer_gamma_sign'];
    if (sign == null) {
      return null;
    }
    const n = typeof sign === 'number' ? sign : Number(sign);
    if (n === -1) {
      return 'dealers short gamma';
    }
    if (n === 1) {
      return 'dealers long gamma';
    }
    return 'undetermined';
  }

  numOpt(key: string): number | null {
    const v = this.card().options_line[key];
    return v == null ? null : Number(v);
  }

  hasOptionsLine(): boolean {
    const o = this.card().options_line;
    return (
      o['verdict'] != null ||
      o['venue'] != null ||
      this.gammaLabel() != null ||
      o['nearest_path_strike'] != null ||
      o['hours_to_expiry'] != null ||
      o['expression'] != null
    );
  }

  hasLocation(): boolean {
    const loc = this.card().location;
    return (
      loc['vwap_distance_atr'] != null ||
      loc['value_area_high_distance_atr'] != null ||
      loc['value_area_low_distance_atr'] != null ||
      loc['chandelier_stop_distance_atr'] != null
    );
  }

  driverValue(v: unknown): string {
    if (typeof v === 'boolean') {
      return v ? 'true' : 'false';
    }
    if (typeof v === 'number') {
      return fmtNum(v);
    }
    if (typeof v === 'string') {
      return v;
    }
    return String(v);
  }
}
