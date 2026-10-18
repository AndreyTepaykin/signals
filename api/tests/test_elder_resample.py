"""30m and daily resampling (the live timeframes) and the kept 6H / weekly ones: UTC boundaries, complete buckets only, no lookahead index."""

from datetime import datetime, timedelta, timezone

import pytest

from app.research.elder.indicators import ema, ohlcv_fields
from app.research.elder.resample import (
    STEP_1D,
    STEP_1H,
    STEP_1W,
    STEP_4H,
    STEP_6H,
    STEP_15M,
    STEP_30M,
    OHLCV,
    align,
    bucket_start_1d,
    bucket_start_1w,
    bucket_start_6h,
    bucket_start_30m,
    last_closed_index,
    to_1d,
    to_1w,
    to_6h,
    to_30m,
)

UTC = timezone.utc
MONDAY = datetime(2026, 1, 5, tzinfo=UTC)  # Monday
NEXT_MONDAY = datetime(2026, 1, 12, tzinfo=UTC)
THIRD_MONDAY = datetime(2026, 1, 19, tzinfo=UTC)


def utc(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


def hour_bar(hour, open_, high, low, close, volume=1.0, day=5, length=STEP_1H):
    opened = utc(2026, 1, day, hour)
    return OHLCV(opened, opened + length, float(open_), float(high), float(low), float(close), float(volume))


def full_day():
    """24 exact hours. Hour h: open h, high h+10, low h-1, close h+0.5, volume h+1."""
    return [hour_bar(hour, hour, hour + 10, hour - 1, hour + 0.5, hour + 1) for hour in range(24)]


def four_hour(monday, index):
    """4h bar i of the week that opens ``monday``. Prices are a pure function of i."""
    opened = monday + index * STEP_4H
    return OHLCV(
        opened,
        opened + STEP_4H,
        100.0 + index,
        200.0 + index,
        50.0 + index,
        150.0 + index,
        1.0,
    )


def week(monday, skip=()):
    return [four_hour(monday, index) for index in range(42) if index not in skip]


def expected_6h(hour):
    """Hand aggregation of hours ``hour .. hour+5`` from :func:`full_day`."""
    hours = range(hour, hour + 6)
    return OHLCV(
        utc(2026, 1, 5, hour),
        utc(2026, 1, 5, hour) + STEP_6H,
        float(hour),
        float(hour + 5 + 10),
        float(hour - 1),
        float(hour + 5 + 0.5),
        float(sum(h + 1 for h in hours)),
    )


# --- steps and bucket floors ----------------------------------------------
def test_step_constants():
    assert STEP_15M == timedelta(minutes=15)
    assert STEP_1H == timedelta(hours=1)
    assert STEP_4H == timedelta(hours=4)
    assert STEP_30M == timedelta(minutes=30)
    assert STEP_6H == timedelta(hours=6)
    assert STEP_1D == timedelta(days=1)
    assert STEP_1W == timedelta(days=7)


@pytest.mark.parametrize(
    'hour,bucket_hour',
    [(0, 0), (5, 0), (6, 6), (11, 6), (12, 12), (17, 12), (18, 18), (23, 18)],
)
def test_6h_floor_is_00_06_12_18(hour, bucket_hour):
    assert bucket_start_6h(utc(2026, 1, 5, hour)) == utc(2026, 1, 5, bucket_hour)
    assert bucket_start_6h(utc(2026, 1, 5, hour, 59)) == utc(2026, 1, 5, bucket_hour)


def test_6h_floor_converts_to_utc_first():
    plus3 = timezone(timedelta(hours=3))
    # 08:00+03 is 05:00 UTC, still inside the 00:00 bucket. 09:00+03 is 06:00 UTC.
    assert bucket_start_6h(datetime(2026, 1, 5, 8, tzinfo=plus3)) == utc(2026, 1, 5, 0)
    assert bucket_start_6h(datetime(2026, 1, 5, 9, tzinfo=plus3)) == utc(2026, 1, 5, 6)


def test_weekly_floor_is_monday_and_sunday_20_belongs_to_it():
    sunday_20 = utc(2026, 1, 11, 20)
    assert sunday_20.strftime('%A') == 'Sunday'
    assert bucket_start_1w(sunday_20) == MONDAY
    assert bucket_start_1w(MONDAY) == MONDAY
    assert bucket_start_1w(NEXT_MONDAY) == NEXT_MONDAY
    # Monday 00:00 UTC is still Sunday evening in +03, so it must not roll forward.
    plus3 = timezone(timedelta(hours=3))
    assert bucket_start_1w(datetime(2026, 1, 12, 2, tzinfo=plus3)) == MONDAY
    assert bucket_start_1w(datetime(2026, 1, 12, 3, tzinfo=plus3)) == NEXT_MONDAY


def test_bucket_floors_reject_naive_datetimes():
    naive = datetime(2026, 1, 5, 5)
    with pytest.raises(ValueError):
        bucket_start_6h(naive)
    with pytest.raises(ValueError):
        bucket_start_1w(naive)


# --- 6H aggregation --------------------------------------------------------
def test_6h_boundaries_and_aggregation():
    built = to_6h(full_day())
    assert [bar.open_time for bar in built] == [
        utc(2026, 1, 5, 0),
        utc(2026, 1, 5, 6),
        utc(2026, 1, 5, 12),
        utc(2026, 1, 5, 18),
    ]
    assert built == [expected_6h(0), expected_6h(6), expected_6h(12), expected_6h(18)]
    # 05:00 belongs to the 00:00 bucket: that bucket closes at the 05:00 bar's close.
    # 06:00 opens the next bucket and is not inside the first.
    assert built[0].close == full_day()[5].close
    assert built[1].open == full_day()[6].open
    assert built[0].close_time == utc(2026, 1, 5, 6)
    assert built[0].close_time == built[0].open_time + STEP_6H


def test_6h_source_given_in_a_non_utc_offset_still_buckets_on_utc():
    plus3 = timezone(timedelta(hours=3))
    bars = []
    for hour in range(3, 9):  # 03:00+03 .. 08:00+03 == 00:00 .. 05:00 UTC
        opened = datetime(2026, 1, 5, hour, tzinfo=plus3)
        bars.append(OHLCV(opened, opened + STEP_1H, 1.0, 2.0, 0.5, 1.5, 1.0))
    built = to_6h(bars)
    assert len(built) == 1
    assert built[0].open_time == utc(2026, 1, 5, 0)
    assert built[0].close_time == utc(2026, 1, 5, 6)


def test_6h_ignores_binance_style_close_and_writes_the_exclusive_end():
    """Source close_time is not copied. The bucket end is open + 6h, not minus 1ms."""
    bars = [hour_bar(hour, 1, 2, 0, hour, length=STEP_1H - timedelta(milliseconds=1)) for hour in range(6)]
    built = to_6h(bars)
    assert len(built) == 1
    assert built[0].close_time == utc(2026, 1, 5, 6)
    assert built[0].close == 5


def test_6h_leading_partial_is_skipped_and_not_merged():
    # Hours 3, 4, 5 do not complete the 00:00 bucket and must not join 06:00.
    bars = [bar for bar in full_day() if 3 <= bar.open_time.hour <= 11]
    assert to_6h(bars) == [expected_6h(6)]


def test_6h_trailing_partial_is_skipped():
    bars = [bar for bar in full_day() if bar.open_time.hour <= 8]
    assert to_6h(bars) == [expected_6h(0)]


def test_6h_hole_inside_a_bucket_skips_that_bucket_only():
    bars = [bar for bar in full_day() if bar.open_time.hour != 3]
    built = to_6h(bars)
    assert [bar.open_time.hour for bar in built] == [6, 12, 18]
    assert built[0] == expected_6h(6)


def test_6h_six_bars_that_do_not_start_on_a_boundary_build_nothing():
    bars = [bar for bar in full_day() if 1 <= bar.open_time.hour <= 6]
    assert to_6h(bars) == []


def test_6h_empty_and_a_single_bar_build_nothing():
    assert to_6h([]) == []
    assert to_6h(full_day()[:1]) == []


def test_6h_rejects_unsorted_duplicate_and_off_grid():
    with pytest.raises(ValueError):
        to_6h([hour_bar(2, 1, 1, 1, 1), hour_bar(1, 1, 1, 1, 1)])
    with pytest.raises(ValueError):
        to_6h([hour_bar(1, 1, 1, 1, 1), hour_bar(1, 2, 2, 2, 2)])
    opened = utc(2026, 1, 5, 0, 30)
    with pytest.raises(ValueError):
        to_6h([OHLCV(opened, opened + STEP_1H, 1, 1, 1, 1, 1)])


def test_6h_rejects_a_naive_open_time():
    opened = datetime(2026, 1, 5, 0)
    bars = [OHLCV(opened, opened + STEP_1H, 1, 1, 1, 1, 1)]
    with pytest.raises(ValueError):
        to_6h(bars)


# --- weekly aggregation ----------------------------------------------------
def test_weekly_bucket_includes_sunday_20_and_excludes_next_monday():
    sunday = four_hour(MONDAY, 41)
    assert sunday.open_time == utc(2026, 1, 11, 20)
    assert bucket_start_1w(sunday.open_time) == MONDAY
    built = to_1w(week(MONDAY) + [four_hour(NEXT_MONDAY, 0)])
    assert len(built) == 1
    assert built[0] == OHLCV(MONDAY, NEXT_MONDAY, 100.0, 241.0, 50.0, 191.0, 42.0)
    # The extra Monday bar would have been the open of a second week; it is not this close.
    assert built[0].close == sunday.close
    assert built[0].open == four_hour(MONDAY, 0).open


def test_weekly_leading_partial_is_skipped_and_not_merged():
    previous = datetime(2025, 12, 29, tzinfo=UTC)
    leading = [four_hour(previous, index) for index in range(39, 42)]
    assert to_1w(leading + week(MONDAY)) == to_1w(week(MONDAY))


def test_weekly_trailing_partial_is_skipped():
    trailing = [four_hour(NEXT_MONDAY, index) for index in range(5)]
    assert to_1w(week(MONDAY) + trailing) == to_1w(week(MONDAY))


def test_weekly_hole_skips_that_week_only():
    built = to_1w(week(MONDAY, skip={10}) + week(NEXT_MONDAY))
    assert [bar.open_time for bar in built] == [NEXT_MONDAY]
    assert built == to_1w(week(NEXT_MONDAY))


def test_two_complete_weeks():
    built = to_1w(week(MONDAY) + week(NEXT_MONDAY))
    assert [bar.open_time for bar in built] == [MONDAY, NEXT_MONDAY]
    assert [bar.close_time for bar in built] == [NEXT_MONDAY, THIRD_MONDAY]


def test_weekly_empty_and_off_grid():
    assert to_1w([]) == []
    opened = utc(2026, 1, 5, 2)  # not a 00/04/08/12/16/20 boundary
    bad = OHLCV(opened, opened + STEP_4H, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError):
        to_1w([bad])
    first, second = four_hour(MONDAY, 1), four_hour(MONDAY, 0)
    with pytest.raises(ValueError):
        to_1w([first, second])
    with pytest.raises(ValueError):
        to_1w([four_hour(MONDAY, 0), four_hour(MONDAY, 0)])


# --- align and the closed-bar index ----------------------------------------
def test_align_inserts_none_for_a_skipped_6h_bucket():
    # Drop hour 9, so the 06:00 bucket is the only one missing.
    built = to_6h([bar for bar in full_day() if bar.open_time.hour != 9])
    assert [bar.open_time.hour for bar in built] == [0, 12, 18]
    aligned = align(built, STEP_6H)
    assert [slot if slot is None else slot.open_time.hour for slot in aligned] == [0, None, 12, 18]
    assert aligned[1] is None
    assert aligned[0] == expected_6h(0)
    assert aligned[2] == expected_6h(12)


def test_align_inserts_none_for_a_skipped_week_and_feeds_a_cold_ema():
    aligned = align(to_1w(week(MONDAY)) + to_1w(week(THIRD_MONDAY)), STEP_1W)
    assert [slot if slot is None else slot.open_time for slot in aligned] == [MONDAY, None, THIRD_MONDAY]
    _opens, _highs, _lows, closes, _volumes = ohlcv_fields(aligned)
    # period 2 cannot bridge the hole: the third week is a new run of length 1.
    assert ema(closes, 2) == [None, None, None]


def test_align_of_a_6h_hole_is_a_null_the_ema_does_not_bridge():
    built = to_6h([bar for bar in full_day() if not 12 <= bar.open_time.hour <= 17])
    aligned = align(built, STEP_6H)
    closes = [None if bar is None else bar.close for bar in aligned]
    # Closes 5.5, 11.5, None, 23.5. EMA(2) seeds at the second bar and dies at the hole.
    assert closes == [5.5, 11.5, None, 23.5]
    warmed = ema(closes, 2)
    assert warmed[0] is None and warmed[2] is None and warmed[3] is None
    assert warmed[1] == pytest.approx(8.5)


def test_align_empty_single_and_rejects_a_broken_grid():
    assert align([], STEP_6H) == []
    only = expected_6h(0)
    assert align([only], STEP_6H) == [only]
    opened = utc(2026, 1, 5, 7)
    off = OHLCV(opened, opened + STEP_6H, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError):
        align([only, off], STEP_6H)
    with pytest.raises(ValueError):
        align([only, only], STEP_6H)
    with pytest.raises(ValueError):
        align([only], timedelta(0))


def test_last_closed_index_boundary():
    start = utc(2026, 1, 5)
    bars = [
        OHLCV(start + i * STEP_6H, start + (i + 1) * STEP_6H, 1, 2, 0, 1, 1)
        for i in range(3)
    ]
    first_close = start + STEP_6H
    second_close = start + 2 * STEP_6H
    # close_time == as_of is closed. One microsecond earlier, that bar is still open.
    assert last_closed_index(bars, first_close) == 0
    assert last_closed_index(bars, first_close - timedelta(microseconds=1)) is None
    assert last_closed_index(bars, second_close) == 1
    assert last_closed_index(bars, second_close - timedelta(microseconds=1)) == 0
    assert last_closed_index(bars, start + 3 * STEP_6H) == 2
    assert last_closed_index(bars, start) is None
    assert last_closed_index([], first_close) is None


def test_last_closed_index_rejects_unsorted_bars_and_a_naive_clock():
    start = utc(2026, 1, 5)
    later = OHLCV(start + STEP_6H, start + 2 * STEP_6H, 1, 1, 1, 1, 1)
    earlier = OHLCV(start, start + STEP_6H, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError):
        last_closed_index([later, earlier], start + 2 * STEP_6H)
    with pytest.raises(ValueError):
        last_closed_index([earlier, later], datetime(2026, 1, 5, 12))


# --- 30m aggregation -------------------------------------------------------
def quarter_bar(hour, minute, open_, high, low, close, volume=1.0, day=5):
    opened = utc(2026, 1, day, hour, minute)
    return OHLCV(opened, opened + STEP_15M, float(open_), float(high), float(low), float(close), float(volume))


def quarter_hours(count=8, start_hour=0):
    """``count`` exact 15m bars from ``start_hour``:00. Bar i: open i, high i+10, low i-1, close i+0.5, volume i+1."""
    out = []
    for i in range(count):
        opened = utc(2026, 1, 5, start_hour) + i * STEP_15M
        out.append(
            OHLCV(opened, opened + STEP_15M, float(i), float(i + 10), float(i - 1), i + 0.5, float(i + 1))
        )
    return out


def expected_30m(first, opened):
    """Hand aggregation of quarter bars ``first`` and ``first + 1`` from :func:`quarter_hours`."""
    a, b = first, first + 1
    return OHLCV(
        opened,
        opened + STEP_30M,
        float(a),
        float(b + 10),
        float(a - 1),
        b + 0.5,
        float((a + 1) + (b + 1)),
    )


@pytest.mark.parametrize(
    'minute,bucket_minute',
    [(0, 0), (14, 0), (15, 0), (29, 0), (30, 30), (44, 30), (45, 30), (59, 30)],
)
def test_30m_floor_is_00_and_30(minute, bucket_minute):
    assert bucket_start_30m(utc(2026, 1, 5, 10, minute)) == utc(2026, 1, 5, 10, bucket_minute)
    assert bucket_start_30m(utc(2026, 1, 5, 10, minute, 59, 999)) == utc(2026, 1, 5, 10, bucket_minute)


def test_30m_floor_converts_to_utc_first_and_rejects_naive():
    plus3 = timezone(timedelta(hours=3))
    assert bucket_start_30m(datetime(2026, 1, 5, 13, 45, tzinfo=plus3)) == utc(2026, 1, 5, 10, 30)
    with pytest.raises(ValueError):
        bucket_start_30m(datetime(2026, 1, 5, 10, 15))


def test_30m_quarter_past_belongs_to_the_hour_and_half_past_starts_the_next():
    built = to_30m(quarter_hours(4))
    assert [bar.open_time for bar in built] == [utc(2026, 1, 5, 0, 0), utc(2026, 1, 5, 0, 30)]
    assert built == [expected_30m(0, utc(2026, 1, 5, 0, 0)), expected_30m(2, utc(2026, 1, 5, 0, 30))]
    # The :15 bar closes the :00 bucket; the :30 bar opens the next one.
    quarters = quarter_hours(4)
    assert built[0].close == quarters[1].close
    assert built[0].close_time == utc(2026, 1, 5, 0, 30)
    assert built[1].open == quarters[2].open
    assert built[1].close_time == built[1].open_time + STEP_30M


def test_30m_ignores_binance_style_close_and_writes_the_exclusive_end():
    bars = []
    for i in range(2):
        opened = utc(2026, 1, 5, 0) + i * STEP_15M
        bars.append(OHLCV(opened, opened + STEP_15M - timedelta(milliseconds=1), 1, 2, 0, i, 1))
    built = to_30m(bars)
    assert len(built) == 1
    assert built[0].close_time == utc(2026, 1, 5, 0, 30)
    assert built[0].close == 1


def test_30m_leading_partial_is_skipped_and_not_merged():
    # The :15 bar alone does not complete the :00 bucket and must not join :30.
    bars = quarter_hours(6)[1:]
    assert to_30m(bars) == [expected_30m(2, utc(2026, 1, 5, 0, 30)), expected_30m(4, utc(2026, 1, 5, 1, 0))]


def test_30m_trailing_partial_is_skipped():
    bars = quarter_hours(5)  # :00 :15 :30 :45 and a lone next-hour :00
    assert to_30m(bars) == [expected_30m(0, utc(2026, 1, 5, 0, 0)), expected_30m(2, utc(2026, 1, 5, 0, 30))]


def test_30m_hole_skips_only_its_bucket():
    bars = [bar for i, bar in enumerate(quarter_hours(8)) if i != 3]  # the 00:45 bar is missing
    built = to_30m(bars)
    assert [bar.open_time for bar in built] == [utc(2026, 1, 5, 0, 0), utc(2026, 1, 5, 1, 0), utc(2026, 1, 5, 1, 30)]
    assert built[0] == expected_30m(0, utc(2026, 1, 5, 0, 0))
    assert built[1] == expected_30m(4, utc(2026, 1, 5, 1, 0))


def test_30m_two_bars_that_straddle_a_boundary_build_nothing():
    assert to_30m(quarter_hours(3)[1:]) == []  # :15 and :30 are in different buckets


def test_30m_empty_and_single_build_nothing():
    assert to_30m([]) == []
    assert to_30m(quarter_hours(1)) == []


def test_30m_rejects_unsorted_duplicate_off_grid_and_naive():
    a, b = quarter_hours(2)
    with pytest.raises(ValueError):
        to_30m([b, a])
    with pytest.raises(ValueError):
        to_30m([a, a])
    opened = utc(2026, 1, 5, 0, 5)
    with pytest.raises(ValueError):
        to_30m([OHLCV(opened, opened + STEP_15M, 1, 1, 1, 1, 1)])
    naive = datetime(2026, 1, 5, 0)
    with pytest.raises(ValueError):
        to_30m([OHLCV(naive, naive + STEP_15M, 1, 1, 1, 1, 1)])


def test_align_30m_makes_a_none_slot_for_a_skipped_bucket():
    bars = [bar for i, bar in enumerate(quarter_hours(8)) if i != 3]
    aligned = align(to_30m(bars), STEP_30M)
    assert [slot if slot is None else slot.open_time for slot in aligned] == [
        utc(2026, 1, 5, 0, 0),
        None,
        utc(2026, 1, 5, 1, 0),
        utc(2026, 1, 5, 1, 30),
    ]


# --- daily aggregation -----------------------------------------------------
def day_of_4h(day_start, skip=()):
    return [four_hour(day_start, index) for index in range(6) if index not in skip]


def test_1d_step_and_floor():
    assert bucket_start_1d(utc(2026, 1, 5, 0)) == utc(2026, 1, 5)
    assert bucket_start_1d(utc(2026, 1, 5, 20)) == utc(2026, 1, 5)
    assert bucket_start_1d(utc(2026, 1, 5, 23, 59, 59, 999999)) == utc(2026, 1, 5)
    assert bucket_start_1d(utc(2026, 1, 6, 0)) == utc(2026, 1, 6)
    plus3 = timezone(timedelta(hours=3))
    assert bucket_start_1d(datetime(2026, 1, 6, 2, tzinfo=plus3)) == utc(2026, 1, 5)
    assert bucket_start_1d(datetime(2026, 1, 6, 3, tzinfo=plus3)) == utc(2026, 1, 6)
    with pytest.raises(ValueError):
        bucket_start_1d(datetime(2026, 1, 5, 5))


def test_1d_bar_at_20_belongs_to_that_day_and_next_midnight_starts_a_new_one():
    day = utc(2026, 1, 5)
    next_day = utc(2026, 1, 6)
    last = four_hour(day, 5)
    assert last.open_time == utc(2026, 1, 5, 20)
    built = to_1d(day_of_4h(day) + [four_hour(next_day, 0)])
    assert len(built) == 1  # the lone 00:00 bar of the next day is a trailing partial
    assert built[0] == OHLCV(day, next_day, 100.0, 205.0, 50.0, 155.0, 6.0)
    assert built[0].close == last.close
    assert built[0].open == four_hour(day, 0).open
    both = to_1d(day_of_4h(day) + day_of_4h(next_day))
    assert [bar.open_time for bar in both] == [day, next_day]
    assert [bar.close_time for bar in both] == [next_day, utc(2026, 1, 7)]


def test_1d_leading_partial_is_skipped_and_not_merged():
    previous = utc(2026, 1, 4)
    leading = [four_hour(previous, index) for index in range(3, 6)]
    day = utc(2026, 1, 5)
    assert to_1d(leading + day_of_4h(day)) == to_1d(day_of_4h(day))
    assert len(to_1d(leading + day_of_4h(day))) == 1


def test_1d_trailing_partial_is_skipped():
    next_day = utc(2026, 1, 6)
    trailing = [four_hour(next_day, index) for index in range(5)]
    day = utc(2026, 1, 5)
    assert to_1d(day_of_4h(day) + trailing) == to_1d(day_of_4h(day))


def test_1d_hole_skips_only_its_day():
    day, next_day = utc(2026, 1, 5), utc(2026, 1, 6)
    built = to_1d(day_of_4h(day, skip={2}) + day_of_4h(next_day))
    assert [bar.open_time for bar in built] == [next_day]
    assert built == to_1d(day_of_4h(next_day))
    third = utc(2026, 1, 7)
    built = to_1d(day_of_4h(day) + day_of_4h(next_day, skip={5}) + day_of_4h(third))
    assert [bar.open_time for bar in built] == [day, third]


def test_1d_six_bars_that_do_not_start_at_midnight_build_nothing():
    bars = [four_hour(utc(2026, 1, 5), index) for index in range(1, 6)] + [four_hour(utc(2026, 1, 6), 0)]
    assert to_1d(bars) == []


def test_1d_empty_and_off_grid():
    assert to_1d([]) == []
    opened = utc(2026, 1, 5, 2)
    with pytest.raises(ValueError):
        to_1d([OHLCV(opened, opened + STEP_4H, 1, 1, 1, 1, 1)])
    first, second = four_hour(utc(2026, 1, 5), 1), four_hour(utc(2026, 1, 5), 0)
    with pytest.raises(ValueError):
        to_1d([first, second])
    with pytest.raises(ValueError):
        to_1d([four_hour(utc(2026, 1, 5), 0), four_hour(utc(2026, 1, 5), 0)])


def test_align_1d_makes_a_none_slot_for_a_skipped_day_and_feeds_a_cold_ema():
    aligned = align(to_1d(day_of_4h(utc(2026, 1, 5)) + day_of_4h(utc(2026, 1, 7))), STEP_1D)
    assert [slot if slot is None else slot.open_time for slot in aligned] == [
        utc(2026, 1, 5),
        None,
        utc(2026, 1, 7),
    ]
    _opens, _highs, _lows, closes, _volumes = ohlcv_fields(aligned)
    assert ema(closes, 2) == [None, None, None]
