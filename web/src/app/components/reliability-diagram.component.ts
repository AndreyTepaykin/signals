import { Component, input } from '@angular/core';
import type { ReliabilityBin } from '../models/api.types';

@Component({
  selector: 'app-reliability-diagram',
  standalone: true,
  template: `
    @if (bins().length === 0) {
      <p>{{ note() }}</p>
    } @else {
      <svg [attr.width]="size" [attr.height]="size" class="chart">
        <text [attr.x]="pad" [attr.y]="pad - 4" font-size="10">Observed</text>
        <text [attr.x]="size - pad - 40" [attr.y]="size - 6" font-size="10">Predicted</text>
        <line [attr.x1]="pad" [attr.y1]="size - pad" [attr.x2]="size - pad" [attr.y2]="pad" stroke="#999" />
        @for (b of bins(); track b.low) {
          @if (b.mean_probability != null && b.observed_rate != null) {
            <circle
              [attr.cx]="pad + b.mean_probability * inner"
              [attr.cy]="size - pad - b.observed_rate * inner"
              [attr.r]="4 + Math.min(b.n_independent / 10, 6)"
              fill="#036"
            />
            <text
              [attr.x]="pad + b.mean_probability * inner"
              [attr.y]="size - pad - b.observed_rate * inner - 8"
              text-anchor="middle"
              font-size="9"
            >
              n={{ b.n_independent }}
            </text>
          }
        }
      </svg>
    }
  `,
  styles: [
    `
      .chart {
        background: #fafafa;
        border: 1px solid #ddd;
      }
    `,
  ],
})
export class ReliabilityDiagramComponent {
  readonly bins = input<ReliabilityBin[]>([]);
  readonly note = input<string>('No calibration bins yet.');
  readonly size = 240;
  readonly pad = 28;
  readonly inner = this.size - 2 * this.pad;
  readonly Math = Math;
}
