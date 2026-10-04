"""Download Deribit options_chain snapshots from Tardis.dev free first-of-month files.

Streams ``OPTIONS.csv.gz`` (no API key), keeps the latest row per symbol in a short UTC
window on the first of the month, converts to ``get_book_summary_by_currency`` shape, and
writes compact gzipped JSON fixtures under ``tests/fixtures/tardis/``.

Unit notes (verified against ``tests/fixtures/deribit_book_summary_btc.json``):
- ``open_interest``: underlying coin units (1 contract = 1 BTC/ETH); Tardis matches Deribit.
- ``mark_iv`` / ``bid_iv`` / ``ask_iv``: annualised implied vol in **percent** (50.0 = 50%);
  ``book_from_payload`` divides by 100 to a fraction.
- ``mark_price``: option premium in **coin units** (BTC/ETH per contract), not USD; Tardis matches.
- ``underlying_price``: forward for that expiry when ``underlying_index`` names an expiry index;
  when ``underlying_index`` is ``index_price``, it is the spot index at that tick.
- ``timestamp``: microseconds since epoch; Deribit ``creation_timestamp`` is milliseconds (// 1000).

``estimated_delivery_price`` (Deribit spot index for the snapshot): median of ``underlying_price``
over rows whose ``underlying_index`` is ``index_price``; if none, median over all kept rows
(documented fallback for sparse index ticks).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
import zlib
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

UTC = timezone.utc
API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = API_ROOT / 'tests' / 'fixtures' / 'tardis'
DEFAULT_COINS = ('BTC', 'ETH')
DEFAULT_MONTHS = tuple((y, m) for y, m in (
    (2025, 10), (2025, 11), (2025, 12),
    *( (2026, m) for m in range(1, 10) ),
))
WINDOW_MINUTES = 15
TARDIS_HEADER = (
    'exchange,symbol,timestamp,local_timestamp,type,strike_price,expiration,open_interest,'
    'last_price,bid_price,bid_amount,bid_iv,ask_price,ask_amount,ask_iv,mark_price,mark_iv,'
    'underlying_index,underlying_price,delta,gamma,vega,theta,rho'
).split(',')


def _month_window(year: int, month: int) -> tuple[int, int, datetime]:
    start_dt = datetime(year, month, 1, 0, 0, tzinfo=UTC)
    end_dt = start_dt + timedelta(minutes=WINDOW_MINUTES)
    return int(start_dt.timestamp() * 1_000_000), int(end_dt.timestamp() * 1_000_000), start_dt


def _url(year: int, month: int) -> str:
    return f'https://datasets.tardis.dev/v1/deribit/options_chain/{year}/{month:02d}/01/OPTIONS.csv.gz'


def _coin_from_symbol(symbol: str) -> str | None:
    if symbol.startswith('BTC-'):
        return 'BTC'
    if symbol.startswith('ETH-'):
        return 'ETH'
    return None


def _float(value: str | None) -> float | None:
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _iter_csv_rows(
    client: httpx.Client,
    url: str,
    *,
    window_start_us: int,
    window_end_us: int,
    timeout: httpx.Timeout,
) -> Iterator[dict[str, str]]:
    """Yield CSV rows with ``window_start_us <= timestamp <= window_end_us``; stop once past the window."""
    delay = 5.0
    for attempt in range(8):
        with client.stream('GET', url, timeout=timeout) as response:
            if response.status_code in (429, 503):
                body_preview = response.read().decode('utf-8', errors='replace')[:400]
                response.close()
                if 'data transfer limit' in body_preview.lower() or '"code": 277' in body_preview:
                    raise RuntimeError(
                        f'Tardis data transfer limit (HTTP {response.status_code}): {body_preview.strip()}'
                    )
                print(
                    f'  rate limited ({response.status_code}), sleeping {delay:.0f}s (attempt {attempt + 1}/8)',
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay = min(delay * 1.8, 120.0)
                continue
            if response.status_code in (401, 403, 404):
                raise httpx.HTTPStatusError(
                    f'{response.status_code} for {url}',
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            buffer = b''
            header: list[str] | None = None
            past_window = False
            for chunk in response.iter_bytes(256 * 1024):
                buffer += decompressor.decompress(chunk)
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line:
                        continue
                    text = line.decode('utf-8', errors='replace')
                    if header is None:
                        header = text.split(',')
                        if header != TARDIS_HEADER:
                            print(f'WARNING: unexpected header (using parsed names): {header}', file=sys.stderr)
                        continue
                    row = dict(zip(header, next(csv.reader([text]))))
                    ts = int(row['timestamp'])
                    if ts < window_start_us:
                        continue
                    if ts > window_end_us:
                        past_window = True
                        break
                    yield row
                if past_window:
                    break
            return
    raise RuntimeError(f'too many 429/503 responses for {url}')


def _collect_snapshot(
    client: httpx.Client,
    year: int,
    month: int,
    coins: tuple[str, ...],
    *,
    timeout: httpx.Timeout,
) -> dict[str, list[dict[str, Any]]]:
    window_start, window_end, _ = _month_window(year, month)
    latest: dict[str, dict[str, str]] = {}
    url = _url(year, month)
    print(f'  streaming {url} window_us=[{window_start}, {window_end}]')
    for row in _iter_csv_rows(client, url, window_start_us=window_start, window_end_us=window_end, timeout=timeout):
        coin = _coin_from_symbol(row['symbol'])
        if coin is None or coin not in coins:
            continue
        symbol = row['symbol']
        prev = latest.get(symbol)
        if prev is None or int(row['timestamp']) >= int(prev['timestamp']):
            latest[symbol] = row

    by_coin: dict[str, list[dict[str, str]]] = {c: [] for c in coins}
    for symbol, row in latest.items():
        coin = _coin_from_symbol(symbol)
        if coin is not None:
            by_coin[coin].append(row)
    return by_coin


def _index_price(rows: list[dict[str, str]]) -> float:
    index_samples = [_float(r['underlying_price']) for r in rows if r.get('underlying_index') == 'index_price']
    index_samples = [x for x in index_samples if x is not None and x > 0.0]
    if index_samples:
        index_samples.sort()
        return index_samples[len(index_samples) // 2]
    forwards = [_float(r['underlying_price']) for r in rows]
    forwards = [x for x in forwards if x is not None and x > 0.0]
    if not forwards:
        raise ValueError('no underlying_price to derive index')
    forwards.sort()
    return forwards[len(forwards) // 2]


def _tardis_row_to_deribit(row: dict[str, str], index_price: float) -> dict[str, Any] | None:
    coin = _coin_from_symbol(row['symbol'])
    if coin is None:
        return None
    oi = _float(row.get('open_interest'))
    forward = _float(row.get('underlying_price'))
    if oi is None or oi <= 0.0 or forward is None or forward <= 0.0:
        return None
    ts_us = int(row['timestamp'])
    mark_iv = _float(row.get('mark_iv'))
    bid = _float(row.get('bid_price'))
    ask = _float(row.get('ask_price'))
    mark = _float(row.get('mark_price'))
    return {
        'instrument_name': row['symbol'],
        'open_interest': oi,
        'mark_iv': 0.0 if mark_iv is None else mark_iv,
        'underlying_price': forward,
        'estimated_delivery_price': index_price,
        'creation_timestamp': ts_us // 1000,
        'mark_price': 0.0 if mark is None else mark,
        'bid_price': bid,
        'ask_price': ask,
        'base_currency': coin,
        'quote_currency': coin,
        'underlying_index': row.get('underlying_index') or '',
        'volume': 0.0,
        'volume_usd': 0.0,
        'volume_notional': 0.0,
    }


def _write_fixture(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'jsonrpc': '2.0', 'result': rows}
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    with gzip.open(path, 'wb', compresslevel=9) as handle:
        handle.write(raw)


def build_month(
    client: httpx.Client,
    year: int,
    month: int,
    coins: tuple[str, ...],
    *,
    timeout: httpx.Timeout,
    out_dir: Path,
) -> list[tuple[str, Path, int, int, float]]:
    """Returns list of (coin, path, instrument_count, file_bytes, total_oi)."""
    collected = _collect_snapshot(client, year, month, coins, timeout=timeout)
    stats: list[tuple[str, Path, int, int, float]] = []
    tag = f'{year}-{month:02d}-01'
    for coin in coins:
        rows_raw = collected.get(coin, [])
        if not rows_raw:
            print(f'  {coin} {tag}: no rows in window')
            continue
        index = _index_price(rows_raw)
        deribit_rows = []
        total_oi = 0.0
        for row in rows_raw:
            converted = _tardis_row_to_deribit(row, index)
            if converted is None:
                continue
            deribit_rows.append(converted)
            total_oi += float(converted['open_interest'])
        deribit_rows.sort(key=lambda r: r['instrument_name'])
        out_path = out_dir / f'deribit_book_summary_{coin.lower()}_{year}{month:02d}01.json.gz'
        _write_fixture(out_path, deribit_rows)
        size = out_path.stat().st_size
        stats.append((coin, out_path, len(deribit_rows), size, total_oi))
        print(f'  {coin} {tag}: {len(deribit_rows)} instruments, OI={total_oi:,.1f}, {size/1024:.1f} KB -> {out_path.name}')
    return stats


def _parse_months(values: list[str]) -> list[tuple[int, int]]:
    if not values:
        return list(DEFAULT_MONTHS)
    out: list[tuple[int, int]] = []
    for item in values:
        text = item.strip()
        if len(text) == 7 and text[4] == '-':
            y, m = int(text[:4]), int(text[5:7])
        elif len(text) == 6:
            y, m = int(text[:4]), int(text[4:6])
        else:
            raise argparse.ArgumentTypeError(f'expected YYYY-MM or YYYYMM, got {item!r}')
        out.append((y, m))
    return out


def _load_fixture(path: Path) -> dict[str, Any]:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def _report(out_dir: Path) -> None:
    sys.path.insert(0, str(API_ROOT))
    from decimal import Decimal

    from app.core import constants as C
    from app.core.options.book import book_from_payload
    from app.core.options import metrics as M
    from app.core.options.check import BookView, CheckInputs, check_options
    from app.core.options.metrics import is_large_expiry
    from app.worker.feeds.deribit import parse_book_summary
    from datetime import timedelta

    files = sorted(out_dir.glob('deribit_book_summary_*.json.gz'))
    if not files:
        print('No fixtures in', out_dir)
        return

    print('\n=== Threshold report (historical Tardis snapshots) ===')
    header = (
        'month\tcoin\tinstruments\ttotal_oi\tfloor\toi_ratio\toi_pass\tconf_full\t'
        'nearest_exp_oi_share\tlarge_exp\tnearest_exp_h\tmax_pain_dist_pct\tatm_iv\tskew_25d\t'
        'gamma_sign\tverdicts_long_short'
    )
    print(header)
    lag = timedelta(seconds=10)
    for path in files:
        name = path.stem.replace('.json', '')
        # deribit_book_summary_btc_20251001
        parts = name.split('_')
        coin_code = parts[3].upper()
        ymd = parts[4]
        year, month, day = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        coin = f'{coin_code}USDT'
        floor = C.OPTIONS_OI_FLOOR_BY_COIN.get(coin)
        body = _load_fixture(path)
        snap = parse_book_summary(body, coin_code, fetched_at=datetime(year, month, day, 0, 16, tzinfo=UTC), publication_lag=lag)
        book = book_from_payload(snap.payload, snap.snapshot_time)
        index = book.index_price
        hm_by_h = {h: M.derive_horizon_metrics(book, h) for h in C.HORIZONS}
        swing = hm_by_h.get('swing') or next(iter(hm_by_h.values()))
        exp_oi = swing.expiry_open_interest or 0.0
        total_oi = swing.total_open_interest
        share = exp_oi / total_oi if total_oi > 0 else 0.0
        large = is_large_expiry(exp_oi, total_oi)
        mp = swing.max_pain_strike
        mp_dist = '' if mp is None else f'{(mp - index) / index * 100:.2f}'
        atm = swing.atm_iv
        atm_s = '' if atm is None else f'{atm * 100:.1f}'
        skew = swing.skew_25d
        skew_s = '' if skew is None else f'{skew * 100:.2f}'
        gamma = swing.dealer_gamma_sign
        oi_ratio = total_oi / floor if floor else float('nan')
        oi_pass = total_oi >= floor if floor else False
        conf_full = total_oi >= C.OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE * floor if floor else False
        exp_h = swing.hours_to_expiry
        exp_h_s = '' if exp_h is None else f'{exp_h:.1f}'

        ref = Decimal(str(index))
        one_r = ref * Decimal('0.02')
        atr = ref * Decimal('0.01')
        strikes = tuple(M.strike_profile(M.near_dated_quotes(book)))
        view = BookView(
            venue='deribit',
            metric_id=1,
            snapshot_time=snap.snapshot_time,
            total_open_interest=total_oi,
            near_dated_open_interest=swing.near_dated_open_interest,
            expiry_time=swing.expiry_time,
            dealer_gamma_sign=gamma,
            gamma_flip_price=swing.gamma_flip_price,
            max_pain_strike=mp,
            top_strike_oi_share=swing.top_strike_oi_share,
            iv_minus_rv_pct=None,
            strikes=strikes,
        )
        verdicts: list[str] = []
        for direction in ('long', 'short'):
            for horizon in C.HORIZONS:
                hm = hm_by_h[horizon]
                v = BookView(
                    venue=view.venue,
                    metric_id=view.metric_id,
                    snapshot_time=view.snapshot_time,
                    total_open_interest=view.total_open_interest,
                    near_dated_open_interest=hm.near_dated_open_interest,
                    expiry_time=hm.expiry_time,
                    dealer_gamma_sign=hm.dealer_gamma_sign,
                    gamma_flip_price=hm.gamma_flip_price,
                    max_pain_strike=hm.max_pain_strike,
                    top_strike_oi_share=hm.top_strike_oi_share,
                    iv_minus_rv_pct=None,
                    strikes=strikes,
                )
                stop = ref - one_r if direction == 'long' else ref + one_r
                target = ref + one_r if direction == 'long' else ref - one_r
                decision = check_options(
                    CheckInputs(
                        coin=coin,
                        direction=direction,
                        decision_time=snap.snapshot_time + timedelta(minutes=5),
                        status='watch',
                        status_reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK,
                        reference_price=ref,
                        one_r=one_r,
                        atr_price=atr,
                        pre_options_stop_price=stop,
                        pre_options_target_price=target,
                        with_break=True,
                        book=v,
                        source='live',
                    )
                )
                verdicts.append(f'{horizon}/{direction[0]}:{decision.verdict}')
        print(
            f'{year}-{month:02d}\t{coin_code}\t{len(book.quotes)}\t{total_oi:,.0f}\t{floor:,.0f}\t'
            f'{oi_ratio:.2f}\t{oi_pass}\t{conf_full}\t{share:.3f}\t{large}\t{exp_h_s}\t{mp_dist}\t'
            f'{atm_s}\t{skew_s}\t{gamma}\t{",".join(verdicts)}'
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Build Deribit book fixtures from Tardis free monthly options_chain files.')
    parser.add_argument('--months', nargs='*', help='YYYY-MM list (default: 12 months from 2025-10 through 2026-09)')
    parser.add_argument('--coins', nargs='*', default=list(DEFAULT_COINS))
    parser.add_argument('--out-dir', type=Path, default=FIXTURE_DIR)
    parser.add_argument('--timeout', type=float, default=600.0, help='HTTP timeout seconds per file')
    parser.add_argument('--report', action='store_true', help='Print threshold table from existing fixtures')
    parser.add_argument('--force', action='store_true', help='Rebuild months whose fixtures already exist')
    args = parser.parse_args(argv)

    if args.report:
        _report(args.out_dir)
        return 0

    months = _parse_months(args.months or [])
    coins = tuple(c.upper() for c in args.coins)
    timeout = httpx.Timeout(args.timeout, connect=30.0)
    all_stats: list[tuple[str, Path, int, int, float]] = []

    headers = {'User-Agent': 'Signals-M7-fixtures/1.0 (dev tool; contact: local)'}
    with httpx.Client(follow_redirects=True, headers=headers) as client:
        for year, month in months:
            existing = [args.out_dir / f'deribit_book_summary_{c.lower()}_{year}{month:02d}01.json.gz' for c in coins]
            if not args.force and all(p.exists() for p in existing):
                print(f'Skipping {year}-{month:02d}-01 (fixtures present; --force to rebuild)')
                continue
            print(f'Processing {year}-{month:02d}-01 ...')
            try:
                all_stats.extend(
                    build_month(client, year, month, coins, timeout=timeout, out_dir=args.out_dir)
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else '?'
                try:
                    target = exc.request.url
                except RuntimeError:
                    target = _url(year, month)
                print(f'FAILED {year}-{month:02d}: HTTP {status} — {target}', file=sys.stderr)
                return 1
            except RuntimeError as exc:
                print(f'FAILED {year}-{month:02d}: {exc}', file=sys.stderr)
                return 1
            except httpx.HTTPError as exc:
                print(f'FAILED {year}-{month:02d}: {exc}', file=sys.stderr)
                return 1
            finally:
                time.sleep(2.0)

    print('\nSummary:')
    for coin, path, count, size, oi in all_stats:
        print(f'  {path.name}: {count} instruments, {size/1024:.1f} KB, total OI {oi:,.1f} {coin}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
