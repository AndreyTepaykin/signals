import { Component, input, output } from '@angular/core';
import type { TrackerRow } from '../models/api.types';
import { pageRange } from '../utils/pagination';
import { fmtR, fmtUtc, hashPrefix } from '../utils/format';

@Component({
  selector: 'app-tracker-table',
  standalone: true,
  template: `
    <div class="pager">
      <button type="button" [disabled]="offset() === 0" (click)="prev.emit()">Prev</button>
      <span>{{ range().from }}–{{ range().to }} of {{ total() }}</span>
      <button type="button" [disabled]="offset() + pageSize() >= total()" (click)="next.emit()">Next</button>
    </div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Coin</th>
            <th>Hzn</th>
            <th>Dir</th>
            <th>Status</th>
            <th>Decision (UTC)</th>
            <th>Fill (UTC)</th>
            <th>Resolved (UTC)</th>
            <th>1st barrier</th>
            <th>R</th>
            <th>price R</th>
            <th>fees R</th>
            <th>fund R</th>
            <th>MAE</th>
            <th>MFE</th>
            <th>h MFE</th>
            <th>h res</th>
            <th>Stop</th>
            <th>Upper</th>
            <th>Cap</th>
            <th>Flags</th>
            <th>Opt verdict</th>
            <th>γ</th>
            <th>flip ATR</th>
            <th>path ATR</th>
            <th>skew</th>
            <th>IV-RV</th>
            <th>Regime</th>
            <th>Expiry hold</th>
            <th>Hashes</th>
          </tr>
        </thead>
        <tbody>
          @for (row of rows(); track row.candidate_id) {
            <tr>
              <td>{{ row.coin }}</td>
              <td>{{ row.horizon }}</td>
              <td>{{ row.direction }}</td>
              <td>{{ row.status }}</td>
              <td class="mono">{{ fmtUtc(row.decision_time) }}</td>
              <td class="mono">{{ fmtUtc(row.fill_time) }}</td>
              <td class="mono">{{ fmtUtc(row.resolved_at) }}</td>
              <td>{{ row.first_barrier }}</td>
              <td class="mono">{{ fmtR(row.stored_r) }}</td>
              <td class="mono">{{ fmtR(row.price_r) }}</td>
              <td class="mono">{{ fmtR(row.fees_r) }}</td>
              <td class="mono">{{ fmtR(row.funding_r) }}</td>
              <td class="mono">{{ fmtR(row.mae_r) }}</td>
              <td class="mono">{{ fmtR(row.mfe_r) }}</td>
              <td class="mono">{{ fmtR(row.hours_to_mfe) }}</td>
              <td class="mono">{{ fmtR(row.hours_to_resolution) }}</td>
              <td class="mono">{{ row.stop_price ?? '—' }}</td>
              <td class="mono">{{ row.upper_barrier_price ?? '—' }}</td>
              <td class="mono">{{ row.target_cap_price ?? '—' }}</td>
              <td>{{ flags(row) }}</td>
              <td>{{ row.options_snapshot?.verdict ?? '—' }}</td>
              <td>{{ row.options_snapshot?.dealer_gamma_sign ?? '—' }}</td>
              <td class="mono">{{ fmtR(row.options_snapshot?.gamma_flip_distance_atr) }}</td>
              <td class="mono">{{ fmtR(row.options_snapshot?.nearest_path_strike_distance_atr) }}</td>
              <td class="mono">{{ fmtR(row.options_snapshot?.skew_25d) }}</td>
              <td class="mono">{{ fmtR(row.options_snapshot?.iv_minus_rv) }}</td>
              <td>{{ row.regime_trend_family }}/{{ row.regime_session }}/{{ row.regime_vol_pct ?? '—' }}</td>
              <td>{{ row.expiry_during_hold === true ? 'yes' : row.expiry_during_hold === false ? 'no' : '—' }}</td>
              <td class="mono" [title]="row.feature_hash + ' ' + row.rule_hash">
                f {{ hashPrefix(row.feature_hash) }} r {{ hashPrefix(row.rule_hash) }}
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>
  `,
  styles: [
    `
      .scroll {
        overflow-x: auto;
      }
      .mono {
        font-family: ui-monospace, monospace;
        font-size: 0.85rem;
      }
      table {
        border-collapse: collapse;
        font-size: 0.85rem;
      }
      th,
      td {
        border: 1px solid #ddd;
        padding: 0.2rem 0.35rem;
        white-space: nowrap;
      }
      .pager {
        display: flex;
        gap: 0.75rem;
        align-items: center;
        margin-bottom: 0.5rem;
      }
    `,
  ],
})
export class TrackerTableComponent {
  readonly rows = input.required<TrackerRow[]>();
  readonly total = input.required<number>();
  readonly offset = input.required<number>();
  readonly pageSize = input.required<number>();
  readonly prev = output<void>();
  readonly next = output<void>();

  readonly fmtR = fmtR;
  readonly fmtUtc = fmtUtc;
  readonly hashPrefix = hashPrefix;

  range() {
    return pageRange(this.offset(), this.pageSize(), this.total());
  }

  flags(row: TrackerRow): string {
    const f: string[] = [];
    if (row.barrier_ambiguous) f.push('ambig');
    if (row.gapped_stop) f.push('gap');
    if (row.funding_partial) f.push('fund∂');
    if (row.no_entry) f.push('no_entry');
    return f.join(', ') || '—';
  }
}
