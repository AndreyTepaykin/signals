import { Component, input } from '@angular/core';
import type { StandDownState } from '../models/api.types';
import { fmtUtc } from '../utils/format';

@Component({
  selector: 'app-stand-down-banner',
  standalone: true,
  template: `
    @if (state()?.active) {
      <div class="banner stand-down">
        <strong>Stand down (UTC)</strong> — {{ state()?.kinds?.join(', ') }}
        @for (w of state()?.active_event_windows ?? []; track w.event_time) {
          <span>{{ w.kind }} until {{ fmtUtc(w.window_end) }}</span>
        }
      </div>
    }
    <aside class="stand-meta">
      <h4>Feed breaches &amp; upcoming windows (UTC)</h4>
      @if ((state()?.feed_breaches?.length ?? 0) === 0 && (state()?.upcoming_event_windows?.length ?? 0) === 0) {
        <p class="muted">No feed breaches; no event windows in the next 24 h.</p>
      }
      <ul>
        @for (b of state()?.feed_breaches ?? []; track b.feed + (b.coin ?? '')) {
          <li>{{ b.feed }} {{ b.coin ?? '' }}: {{ b.reason }} ({{ b.staleness_seconds ?? '?' }}s / limit {{ b.max_staleness_seconds ?? '?' }}s)</li>
        }
        @for (w of state()?.upcoming_event_windows ?? []; track w.event_time) {
          <li>Upcoming {{ w.kind }} at {{ fmtUtc(w.event_time) }} (window {{ fmtUtc(w.window_start) }} – {{ fmtUtc(w.window_end) }})</li>
        }
      </ul>
    </aside>
  `,
  styles: [
    `
      .banner {
        padding: 0.75rem 1rem;
        &.stand-down {
          background: #fdd;
          border-bottom: 2px solid #c00;
        }
      }
      .stand-meta {
        padding: 0.5rem 1rem;
        font-size: 0.85rem;
        background: #f8f8f8;
        border-bottom: 1px solid #ddd;
      }
      .muted {
        color: #666;
      }
    `,
  ],
})
export class StandDownBannerComponent {
  readonly state = input.required<StandDownState | null>();
  readonly fmtUtc = fmtUtc;
}
