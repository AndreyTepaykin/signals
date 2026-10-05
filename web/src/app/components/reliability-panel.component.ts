import { Component, input, signal } from '@angular/core';
import type { CalibrationCohort } from '../models/api.types';
import { ReliabilityDiagramComponent } from './reliability-diagram.component';

@Component({
  selector: 'app-reliability-panel',
  standalone: true,
  imports: [ReliabilityDiagramComponent],
  template: `
    @for (c of cohorts(); track c.cohort) {
      <article class="cohort">
        <h3>{{ c.cohort.replaceAll('/', ' / ') }}</h3>
        <label>
          Dataset
          <select [value]="mode()" (change)="onMode($event)">
            <option value="alerts">Alerts only</option>
            <option value="all">All statuses</option>
          </select>
        </label>
        <p class="mono">
          Brier: {{ c.brier ?? 'n/a' }} — {{ c.brier_note }} (n with prob:
          {{ active(c).n_independent_with_probability }})
        </p>
        <app-reliability-diagram [bins]="active(c).bins" [note]="active(c).note" />
      </article>
    }
  `,
  styles: [
    `
      .cohort {
        margin-bottom: 1rem;
      }
      .mono {
        font-family: ui-monospace, monospace;
        font-size: 0.85rem;
      }
    `,
  ],
})
export class ReliabilityPanelComponent {
  readonly cohorts = input.required<CalibrationCohort[]>();
  readonly mode = signal<'alerts' | 'all'>('alerts');

  onMode(ev: Event): void {
    const v = (ev.target as HTMLSelectElement).value;
    this.mode.set(v === 'all' ? 'all' : 'alerts');
  }

  active(c: CalibrationCohort) {
    return this.mode() === 'all' ? c.reliability_all_statuses : c.reliability;
  }
}
