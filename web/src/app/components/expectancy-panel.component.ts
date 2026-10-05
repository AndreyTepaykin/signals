import { Component, input } from '@angular/core';
import type { BaselinesCohort, CellSummary, ExpectancyCohort } from '../models/api.types';
import { fmtR } from '../utils/format';

export interface ExpectancyPanelCohort {
  label: string;
  expectancy: ExpectancyCohort;
  baselines: BaselinesCohort;
}

@Component({
  selector: 'app-expectancy-panel',
  standalone: true,
  template: `
    @for (c of cohorts(); track c.label) {
      <article class="cohort">
        <h3>{{ c.label.replaceAll('/', ' / ') }}</h3>
        @if (c.baselines.flags.length) {
          <ul class="flags">
            @for (f of c.baselines.flags; track f) {
              <li>{{ f }}</li>
            }
          </ul>
        }
        @for (cmp of c.baselines.comparisons; track cmp.scope) {
          <div class="scope">
            <h4>{{ cmp.scope === 'all' ? 'All horizons' : cmp.scope }}</h4>
            <table>
              <thead>
                <tr>
                  <th>Population</th>
                  <th>E[R]</th>
                  <th>90% interval</th>
                  <th>n (ind / raw)</th>
                  <th>vs flat</th>
                  <th>vs swing-break</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>App alerts</td>
                  <td class="mono">{{ fmtR(cmp.app.expectancy_r) }}</td>
                  <td class="mono">[{{ fmtR(cmp.app.interval_low) }}, {{ fmtR(cmp.app.interval_high) }}]</td>
                  <td>
                    {{ cmp.app.n_filled_independent }} / {{ cmp.app.n_filled_raw }}
                    @if (cmp.app.insufficient_sample) {
                      <span class="badge">insufficient sample</span>
                    }
                  </td>
                  <td>{{ cmp.vs_flat }}</td>
                  <td>{{ cmp.vs_swing_break }}</td>
                </tr>
                <tr>
                  <td>Always flat</td>
                  <td class="mono">0.000</td>
                  <td>—</td>
                  <td>—</td>
                  <td colspan="2">reference</td>
                </tr>
                <tr>
                  <td>Naive swing-break</td>
                  <td class="mono">{{ fmtR(cmp.baseline.expectancy_r ?? cmp.baseline.summary?.expectancy_r) }}</td>
                  <td class="mono">
                    @if (cmp.baseline.summary) {
                      [{{ fmtR(cmp.baseline.summary.interval_low) }}, {{ fmtR(cmp.baseline.summary.interval_high) }}]
                    } @else {
                      —
                    }
                  </td>
                  <td>
                    @if (cmp.baseline.summary) {
                      {{ cmp.baseline.summary.n_filled_independent }} / {{ cmp.baseline.summary.n_filled_raw }}
                    }
                  </td>
                  <td colspan="2">baseline cohort {{ cmp.baseline.key }}</td>
                </tr>
                <tr>
                  <td>Shadow</td>
                  <td class="mono">{{ fmtR(shadowCell(c, cmp.scope).expectancy_r) }}</td>
                  <td class="mono">
                    [{{ fmtR(shadowCell(c, cmp.scope).interval_low) }},
                    {{ fmtR(shadowCell(c, cmp.scope).interval_high) }}]
                  </td>
                  <td>
                    {{ shadowCell(c, cmp.scope).n_filled_independent }} /
                    {{ shadowCell(c, cmp.scope).n_filled_raw }}
                  </td>
                  <td colspan="2">non-alert population</td>
                </tr>
              </tbody>
            </table>
          </div>
        }
        @if (c.baselines.kill_rule) {
          <p class="kill binding">
            Kill rule (binding): {{ c.baselines.kill_rule.status }} — {{ c.baselines.kill_rule.note }}
          </p>
        }
        @if (c.baselines.kill_rule_preview) {
          <p class="kill preview">
            Kill rule preview: {{ c.baselines.kill_rule_preview.status }} — {{ c.baselines.kill_rule_preview.note }}
          </p>
        }
        <p class="mono small">
          Would abstain: {{ c.baselines.would_abstain.would_abstain_independent }} independent /
          {{ c.baselines.would_abstain.would_abstain }} raw alerts
        </p>
      </article>
    }
  `,
  styles: [
    `
      .cohort {
        border: 1px solid #ccc;
        padding: 0.75rem;
        margin-bottom: 1rem;
      }
      .mono {
        font-family: ui-monospace, monospace;
      }
      .badge {
        background: #fec;
        padding: 0 0.25rem;
        margin-left: 0.25rem;
        font-size: 0.75rem;
      }
      .flags {
        font-size: 0.85rem;
        color: #633;
      }
      .kill.binding {
        color: #900;
      }
      .kill.preview {
        color: #666;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
        margin: 0.5rem 0;
      }
      th,
      td {
        border: 1px solid #ddd;
        padding: 0.25rem;
        text-align: left;
      }
    `,
  ],
})
export class ExpectancyPanelComponent {
  readonly cohorts = input.required<ExpectancyPanelCohort[]>();
  readonly fmtR = fmtR;

  shadowCell(c: ExpectancyPanelCohort, scope: string): CellSummary {
    return scope === 'all' ? c.expectancy.shadow.overall : c.expectancy.shadow.by_horizon[scope];
  }
}
