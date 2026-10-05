import { Component, computed, inject, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ExpectancyPanelComponent, type ExpectancyPanelCohort } from './components/expectancy-panel.component';
import { ReliabilityPanelComponent } from './components/reliability-panel.component';
import { SignalCardComponent } from './components/signal-card.component';
import { StandDownBannerComponent } from './components/stand-down-banner.component';
import { TrackerTableComponent } from './components/tracker-table.component';
import type {
  CalibrationCohort,
  FeedHealthRow,
  SignalCard,
  Source,
  TrackerRow,
} from './models/api.types';
import { ApiService } from './services/api.service';
import { LiveService } from './services/live.service';
import { pageRange } from './utils/pagination';

const CARD_STATUSES = ['', 'alert', 'watch', 'cancelled', 'delayed', 'abstained'] as const;
const TRACKER_PAGE = 50;

@Component({
  selector: 'app-root',
  imports: [
    FormsModule,
    SignalCardComponent,
    TrackerTableComponent,
    ExpectancyPanelComponent,
    ReliabilityPanelComponent,
    StandDownBannerComponent,
  ],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App implements OnInit {
  private readonly api = inject(ApiService);
  private readonly live = inject(LiveService);

  protected readonly cardStatuses = CARD_STATUSES;
  protected readonly source = signal<Source>('live');
  protected readonly cards = signal<SignalCard[]>([]);
  protected readonly cardsTotal = signal(0);
  protected readonly cardsOffset = signal(0);
  protected readonly cardsLimit = signal(50);
  protected readonly cardsEmpty = signal('');
  protected readonly statusFilter = signal('');
  protected readonly coinFilter = signal('');

  protected readonly trackerRows = signal<TrackerRow[]>([]);
  protected readonly trackerTotal = signal(0);
  protected readonly trackerOffset = signal(0);
  protected readonly trackerPageSize = signal(TRACKER_PAGE);

  protected readonly feedRows = signal<FeedHealthRow[]>([]);
  protected readonly expectancyCohorts = signal<ExpectancyPanelCohort[]>([]);
  protected readonly calibrationCohorts = signal<CalibrationCohort[]>([]);
  protected readonly statsLoading = signal(false);
  private statsInFlight = false;

  protected readonly standDown = this.live.standDown;
  protected readonly wsConnected = this.live.connected;

  protected readonly cardsPageLabel = computed(() =>
    pageRange(this.cardsOffset(), this.cardsLimit(), this.cardsTotal()),
  );

  ngOnInit(): void {
    this.live.connect();
    this.refreshAll();
    this.api.standDown().subscribe((s) => this.live.standDown.set(s));
    this.live.onEvent((ev) => {
      if (ev.type === 'candidates') {
        this.loadCards();
      }
      if (ev.type === 'labels') {
        this.loadTracker();
        this.loadStats();
      }
      if (ev.type === 'feed_health') {
        this.loadFeedHealth();
      }
    });
  }

  setSource(next: Source): void {
    this.source.set(next);
    this.cardsOffset.set(0);
    this.trackerOffset.set(0);
    this.refreshAll();
  }

  refreshAll(): void {
    this.loadCards();
    this.loadTracker();
    this.loadFeedHealth();
    this.loadStats();
  }

  loadCards(): void {
    const src = this.source();
    this.api
      .cards(src, {
        status: this.statusFilter() || undefined,
        coin: this.coinFilter().trim() || undefined,
        limit: this.cardsLimit(),
        offset: this.cardsOffset(),
      })
      .subscribe({
        next: (r) => {
          this.cards.set(r.items);
          this.cardsTotal.set(r.total);
          this.cardsEmpty.set(
            r.total === 0 ? (src === 'live' ? 'No live candidates yet' : 'No replay candidates in range') : '',
          );
        },
      });
  }

  cardsPrev(): void {
    this.cardsOffset.update((o) => Math.max(0, o - this.cardsLimit()));
    this.loadCards();
  }

  cardsNext(): void {
    if (this.cardsOffset() + this.cardsLimit() < this.cardsTotal()) {
      this.cardsOffset.update((o) => o + this.cardsLimit());
      this.loadCards();
    }
  }

  loadTracker(): void {
    this.api.tracker(this.source(), this.trackerOffset(), this.trackerPageSize()).subscribe((r) => {
      this.trackerRows.set(r.items);
      this.trackerTotal.set(r.total);
    });
  }

  trackerPrev(): void {
    this.trackerOffset.update((o) => Math.max(0, o - this.trackerPageSize()));
    this.loadTracker();
  }

  trackerNext(): void {
    if (this.trackerOffset() + this.trackerPageSize() < this.trackerTotal()) {
      this.trackerOffset.update((o) => o + this.trackerPageSize());
      this.loadTracker();
    }
  }

  loadFeedHealth(): void {
    this.api.feedHealth().subscribe((r) => this.feedRows.set(r.rows));
  }

  loadStats(): void {
    if (this.statsInFlight) {
      return;
    }
    this.statsInFlight = true;
    this.statsLoading.set(true);
    const src = this.source();
    let expDone = false;
    let baseDone = false;
    let calDone = false;
    const finish = () => {
      if (expDone && baseDone && calDone) {
        this.statsInFlight = false;
        this.statsLoading.set(false);
      }
    };
    this.api.statsExpectancy(src).subscribe({
      next: (exp) => {
        this.api.statsBaselines(src).subscribe({
          next: (base) => {
            const byLabel = new Map(base.cohorts.map((c) => [c.cohort, c]));
            const merged: ExpectancyPanelCohort[] = exp.cohorts.map((e) => ({
              label: e.cohort,
              expectancy: e,
              baselines: byLabel.get(e.cohort) ?? {
                cohort: e.cohort,
                comparisons: [],
                kill_rule: null,
                kill_rule_preview: null,
                would_abstain: {
                  alerts: 0,
                  would_abstain: 0,
                  alerts_independent: 0,
                  would_abstain_independent: 0,
                },
                flags: e.flags,
              },
            }));
            this.expectancyCohorts.set(merged);
            expDone = true;
            baseDone = true;
            finish();
          },
          error: () => {
            expDone = baseDone = true;
            finish();
          },
        });
      },
      error: () => {
        expDone = true;
        baseDone = true;
        finish();
      },
    });
    this.api.statsCalibration(src).subscribe({
      next: (cal) => {
        this.calibrationCohorts.set(cal.cohorts);
        calDone = true;
        finish();
      },
      error: () => {
        calDone = true;
        finish();
      },
    });
  }
}
