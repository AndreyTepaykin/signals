/** Inclusive 1-based display range for a page. */
export function pageRange(offset: number, pageSize: number, total: number): { from: number; to: number } {
  if (total === 0) {
    return { from: 0, to: 0 };
  }
  const from = offset + 1;
  const to = Math.min(offset + pageSize, total);
  return { from, to };
}
