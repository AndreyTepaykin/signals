import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import type {
  BaselinesResponse,
  CalibrationResponse,
  CardListResponse,
  ExpectancyResponse,
  FeedHealthResponse,
  Source,
  StandDownState,
  TrackerListResponse,
} from '../models/api.types';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  cards(
    source: Source,
    opts?: { status?: string; coin?: string; limit?: number; offset?: number },
  ): Observable<CardListResponse> {
    let params = new HttpParams().set('source', source);
    params = params.set('limit', String(opts?.limit ?? 50));
    params = params.set('offset', String(opts?.offset ?? 0));
    if (opts?.status) {
      params = params.set('status', opts.status);
    }
    if (opts?.coin) {
      params = params.set('coin', opts.coin);
    }
    return this.http.get<CardListResponse>('/api/cards', { params });
  }

  tracker(source: Source, offset = 0, limit = 50): Observable<TrackerListResponse> {
    const params = new HttpParams()
      .set('source', source)
      .set('limit', String(limit))
      .set('offset', String(offset));
    return this.http.get<TrackerListResponse>('/api/tracker', { params });
  }

  standDown(): Observable<StandDownState> {
    return this.http.get<StandDownState>('/api/stand-down');
  }

  feedHealth(): Observable<FeedHealthResponse> {
    return this.http.get<FeedHealthResponse>('/api/feed-health');
  }

  statsExpectancy(source: Source): Observable<ExpectancyResponse> {
    return this.http.get<ExpectancyResponse>('/api/stats/expectancy', { params: { source } });
  }

  statsBaselines(source: Source): Observable<BaselinesResponse> {
    return this.http.get<BaselinesResponse>('/api/stats/baselines', { params: { source } });
  }

  statsCalibration(source: Source): Observable<CalibrationResponse> {
    return this.http.get<CalibrationResponse>('/api/stats/calibration', { params: { source } });
  }
}
