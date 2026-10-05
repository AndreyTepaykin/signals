export function fmtR(v: number | null | undefined, digits = 3): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(digits);
}

/** Prices: at least four significant digits. */
export function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) {
    return '—';
  }
  return String(Number(v.toPrecision(4)));
}

export function fmtNum(v: unknown, digits = 2): string {
  if (v == null || typeof v === 'boolean') {
    return String(v);
  }
  const n = typeof v === 'number' ? v : Number(v);
  if (Number.isNaN(n)) {
    return String(v);
  }
  return n.toFixed(digits);
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(digits)}%`;
}

export function hashPrefix(h: string): string {
  return h.length >= 8 ? h.slice(0, 8) : h;
}

export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) {
    return '—';
  }
  const d = new Date(iso);
  return `${d.toISOString().replace('T', ' ').slice(0, 19)} UTC`;
}
