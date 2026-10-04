"""M5 metrics and check over real Deribit books from Tardis monthly fixtures."""

from __future__ import annotations

import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core import constants as C
from app.core.options.book import book_from_payload
from app.core.options.check import BookView, CheckInputs, check_options
from app.core.options import metrics as M
from app.worker.feeds.deribit import parse_book_summary

FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'tardis'
LAG = timedelta(seconds=10)
SKIP_REASON = 'no Tardis fixtures under tests/fixtures/tardis (run scripts/tardis_options_fixtures.py)'


def _fixture_paths() -> list[Path]:
    if not FIXTURE_DIR.is_dir():
        return []
    return sorted(FIXTURE_DIR.glob('deribit_book_summary_*.json.gz'))


def _load(path: Path) -> dict:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def _coin_from_path(path: Path) -> tuple[str, str]:
    stem = path.name.replace('.json.gz', '')
    parts = stem.split('_')
    currency = parts[3].upper()
    return f'{currency}USDT', currency


def _snapshot_guess(path: Path) -> datetime:
    ymd = path.stem.split('_')[-1].replace('.json', '')
    return datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), 0, 16, tzinfo=timezone.utc)


def _book_view(book, hm, snap, metric_id: int = 1) -> BookView:
    strikes = tuple(M.strike_profile(M.near_dated_quotes(book)))
    return BookView(
        venue='deribit',
        metric_id=metric_id,
        snapshot_time=snap.snapshot_time,
        total_open_interest=hm.total_open_interest,
        near_dated_open_interest=hm.near_dated_open_interest,
        expiry_time=hm.expiry_time,
        dealer_gamma_sign=hm.dealer_gamma_sign,
        gamma_flip_price=hm.gamma_flip_price,
        max_pain_strike=hm.max_pain_strike,
        top_strike_oi_share=hm.top_strike_oi_share,
        iv_minus_rv_pct=None,
        strikes=strikes,
    )


def test_historical_book_metrics_are_sane():
    paths = _fixture_paths()
    if not paths:
        pytest.skip(SKIP_REASON)
    for path in paths:
        coin, currency = _coin_from_path(path)
        body = _load(path)
        fetched = parse_book_summary(body, currency, fetched_at=_snapshot_guess(path), publication_lag=LAG)
        book = book_from_payload(fetched.payload, fetched.snapshot_time)
        assert book.total_open_interest > 0.0
        assert 200 <= len(book.quotes) <= 2500, f'{path.name}: unexpected instrument count {len(book.quotes)}'
        strikes = [q.strike for q in book.quotes]
        assert min(strikes) < book.index_price < max(strikes)

        for horizon in C.HORIZONS:
            hm = M.derive_horizon_metrics(book, horizon)
            assert hm.total_open_interest == book.total_open_interest
            assert hm.near_dated_open_interest >= 0.0
            if hm.expiry_time is not None:
                assert hm.hours_to_expiry is not None and hm.hours_to_expiry > 0.0
                assert hm.expiry_open_interest is not None and hm.expiry_open_interest > 0.0
            if hm.max_pain_strike is not None:
                assert min(strikes) <= hm.max_pain_strike <= max(strikes)
            if hm.atm_iv is not None:
                assert hm.atm_iv > 0.0 and math.isfinite(hm.atm_iv)
            if hm.skew_25d is not None:
                assert math.isfinite(hm.skew_25d)
        _ = coin  # coin parsed for future assertions


def test_historical_check_options_verdicts_and_invariants():
    paths = _fixture_paths()
    if not paths:
        pytest.skip(SKIP_REASON)
    for path in paths:
        coin, currency = _coin_from_path(path)
        body = _load(path)
        snap = parse_book_summary(body, currency, fetched_at=_snapshot_guess(path), publication_lag=LAG)
        book = book_from_payload(snap.payload, snap.snapshot_time)
        index = Decimal(str(book.index_price))
        one_r = index * Decimal('0.02')
        atr = index * Decimal('0.01')
        decision_time = snap.snapshot_time + timedelta(minutes=5)

        for horizon in C.HORIZONS:
            hm = M.derive_horizon_metrics(book, horizon)
            view = _book_view(book, hm, snap)
            for direction in ('long', 'short'):
                stop = index - one_r if direction == 'long' else index + one_r
                target = index + one_r if direction == 'long' else index - one_r
                d = check_options(
                    CheckInputs(
                        coin=coin,
                        direction=direction,
                        decision_time=decision_time,
                        status='watch',
                        status_reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK,
                        reference_price=index,
                        one_r=one_r,
                        atr_price=atr,
                        pre_options_stop_price=stop,
                        pre_options_target_price=target,
                        with_break=True,
                        book=view,
                        source='live',
                    )
                )
                assert d.verdict in C.OPTIONS_VERDICT_PRECEDENCE
                assert d.size_multiplier > 0.0
                assert -1.0 <= d.influence <= 1.0
                assert d.confidence is None or 0.0 <= d.confidence <= 1.0
                assert d.expression in C.OPTIONS_EXPRESSIONS
                assert d.vol_tag in (None, *C.OPTIONS_VOL_TAGS)
                assert d.influence == C.OPTIONS_INFLUENCE_BY_VERDICT[d.verdict]
                if d.verdict != 'ignore':
                    assert d.metric_id is not None
                    assert d.stop_price < index if direction == 'long' else d.stop_price > index
