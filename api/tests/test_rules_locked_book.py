"""The 1-in-5 locked book: deterministic, outcome-free, about one in five."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.core import constants as C
from app.core.rules.locked_book import is_locked_book, locked_book_bucket, locked_book_text

UTC = timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
COINS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT')


def test_the_canonical_text_is_the_serialized_coin_and_bar_close():
    assert locked_book_text('BTCUSDT', T0) == '{"bar_close":T"2026-01-01T12:00:00.000000+00:00","coin":"BTCUSDT"}'


def test_the_bucket_is_the_spec_hash_of_that_text_written_out_independently():
    text = '{"bar_close":T"2026-01-01T12:00:00.000000+00:00","coin":"BTCUSDT"}'
    digest = hashlib.sha256(text.encode('utf-8')).digest()
    assert C.HASH_ALGORITHM == 'sha256' and C.LOCKED_BOOK_ONE_IN == 5 and C.LOCKED_BOOK_HASH_BYTES == 8
    assert locked_book_bucket('BTCUSDT', T0) == int.from_bytes(digest[:8], 'big') % 5


def test_pinned_buckets_never_move():
    # If this fails the locked book has been silently redrawn: stored candidates would disagree with the rule.
    got = [locked_book_bucket('BTCUSDT', T0 + timedelta(minutes=15 * i)) for i in range(12)]
    assert got == [3, 1, 0, 1, 3, 3, 2, 1, 4, 1, 3, 2]
    assert [is_locked_book('BTCUSDT', T0 + timedelta(minutes=15 * i)) for i in range(4)] == [False, False, True, False]


def test_it_is_repeatable_and_does_not_depend_on_the_input_timezone():
    other_zone = T0.astimezone(timezone(timedelta(hours=3)))
    assert locked_book_bucket('BTCUSDT', other_zone) == locked_book_bucket('BTCUSDT', T0)
    assert locked_book_bucket('BTCUSDT', T0) == locked_book_bucket('BTCUSDT', T0)


def test_it_rejects_a_naive_bar_close():
    with pytest.raises(ValueError):
        locked_book_bucket('BTCUSDT', datetime(2026, 1, 1, 12, 0))


def test_it_depends_on_the_coin_and_the_bar_only():
    buckets = {locked_book_bucket(coin, T0) for coin in COINS}
    assert len(buckets) > 1  # coins are not all in or out together
    # the function has no horizon / direction / status / outcome parameter at all
    import inspect

    assert list(inspect.signature(locked_book_bucket).parameters) == ['coin', 'bar_close']
    assert list(inspect.signature(is_locked_book).parameters) == ['coin', 'bar_close']


def test_one_in_five_overall_and_per_coin():
    bars = [T0 + timedelta(minutes=15 * i) for i in range(96 * 60)]  # 60 days of 15m bars
    total = hits = 0
    for coin in COINS:
        coin_hits = sum(is_locked_book(coin, bar) for bar in bars)
        assert 0.18 < coin_hits / len(bars) < 0.22, (coin, coin_hits / len(bars))
        total += len(bars)
        hits += coin_hits
    assert 0.19 < hits / total < 0.21


def test_buckets_are_roughly_uniform():
    bars = [T0 + timedelta(minutes=15 * i) for i in range(96 * 30)]
    counts = [0] * 5
    for bar in bars:
        counts[locked_book_bucket('ETHUSDT', bar)] += 1
    assert all(0.17 < count / len(bars) < 0.23 for count in counts), counts


def test_only_the_selected_bucket_is_locked():
    assert C.LOCKED_BOOK_SELECTED_BUCKET == 0
    for i in range(200):
        bar = T0 + timedelta(minutes=15 * i)
        assert is_locked_book('BTCUSDT', bar) == (locked_book_bucket('BTCUSDT', bar) == 0)


def test_the_same_answer_in_another_process_with_another_hash_seed():
    code = (
        'from datetime import datetime, timedelta, timezone;'
        'from app.core.rules.locked_book import locked_book_bucket as b;'
        't=datetime(2026,1,1,12,0,tzinfo=timezone.utc);'
        "print([b('SOLUSDT', t+timedelta(minutes=15*i)) for i in range(40)])"
    )
    outputs = set()
    for seed in ('0', '1', '12345'):
        env = {**os.environ, 'PYTHONHASHSEED': seed, 'TZ': 'Asia/Tokyo'}
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, check=True, cwd=ROOT)
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1
    here = [locked_book_bucket('SOLUSDT', T0 + timedelta(minutes=15 * i)) for i in range(40)]
    assert outputs == {str(here)}
