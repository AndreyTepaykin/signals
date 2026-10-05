import { Injectable, signal } from '@angular/core';
import type { StandDownState } from '../models/api.types';

export type LiveEventType = 'candidates' | 'labels' | 'stand_down' | 'feed_health' | 'connected' | 'disconnected';

export interface LiveEvent {
  type: LiveEventType;
  standDown?: StandDownState;
}

@Injectable({ providedIn: 'root' })
export class LiveService {
  readonly connected = signal(false);
  readonly standDown = signal<StandDownState | null>(null);

  private ws: WebSocket | null = null;
  private backoffMs = 1000;
  private listeners = new Set<(event: LiveEvent) => void>();

  connect(): void {
    if (this.ws) {
      return;
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/api/ws`;
    this.ws = new WebSocket(url);
    this.ws.onopen = () => {
      this.connected.set(true);
      this.backoffMs = 1000;
      this.emit({ type: 'connected' });
    };
    this.ws.onclose = () => {
      this.connected.set(false);
      this.ws = null;
      this.emit({ type: 'disconnected' });
      setTimeout(() => this.connect(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, 30000);
    };
    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as {
          type: string;
          stand_down?: StandDownState;
        };
        if (msg.type === 'hello' && msg.stand_down) {
          this.standDown.set(msg.stand_down);
        }
        if (msg.type === 'stand_down') {
          if (msg.stand_down) {
            this.standDown.set(msg.stand_down);
          }
          this.emit({ type: 'stand_down', standDown: msg.stand_down });
          return;
        }
        if (msg.type === 'candidates' || msg.type === 'labels' || msg.type === 'feed_health') {
          this.emit({ type: msg.type });
        }
      } catch {
        /* ignore */
      }
    };
  }

  onEvent(fn: (event: LiveEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Unit tests only: simulate a server push without a socket. */
  dispatchForTest(event: LiveEvent): void {
    this.emit(event);
  }

  private emit(event: LiveEvent): void {
    for (const fn of this.listeners) {
      fn(event);
    }
  }
}
