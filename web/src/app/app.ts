import { Component, inject, OnInit, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';

interface HealthResponse {
  status: string;
  database?: string | null;
  postgres?: string | null;
  detail?: string | null;
}

@Component({
  selector: 'app-root',
  imports: [],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App implements OnInit {
  private readonly http = inject(HttpClient);

  protected readonly title = signal('Signals');
  protected readonly health = signal<HealthResponse | null>(null);
  protected readonly healthError = signal<string | null>(null);
  protected readonly isLoading = signal(true);

  ngOnInit(): void {
    this.http.get<HealthResponse>('/api/health').subscribe({
      next: (response) => {
        this.health.set(response);
        this.healthError.set(null);
        this.isLoading.set(false);
      },
      error: (err: unknown) => {
        const message =
          err && typeof err === 'object' && 'message' in err
            ? String((err as { message: unknown }).message)
            : 'API unreachable';
        this.healthError.set(message);
        this.isLoading.set(false);
      },
    });
  }
}
