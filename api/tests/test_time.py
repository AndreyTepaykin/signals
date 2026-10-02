from datetime import date, datetime, timedelta, timezone

import pytest

from app.core import constants as C
from app.core import time as T


def utc(y, mo, d, h=0, mi=0, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc)


# 2026-01-14 is a Wednesday (US standard time); 2026-07-15 is a Wednesday (US daylight time).
WINTER_WED = (2026, 1, 14)
SUMMER_WED = (2026, 7, 15)


# --- sessions -------------------------------------------------------------
@pytest.mark.parametrize(
    'hour,minute,bucket',
    [
        (0, 0, 'asia'),
        (6, 59, 'asia'),
        (7, 0, 'europe'),
        (12, 59, 'europe'),
        (13, 0, 'us'),
        (20, 59, 'us'),
        (21, 0, 'late'),
        (23, 59, 'late'),
    ],
)
def test_session_buckets_half_open(hour, minute, bucket):
    info = T.classify_session(utc(*WINTER_WED, hour, minute))
    assert info.bucket == bucket


def test_non_late_session_keeps_its_own_date():
    info = T.classify_session(utc(*WINTER_WED, 9, 30))
    assert (info.session, info.session_date, info.folded) == ('europe', date(2026, 1, 14), False)


def test_late_session_folds_into_next_asia_date():
    info = T.classify_session(utc(2026, 1, 14, 22, 15))
    assert info.bucket == 'late'
    assert info.session == 'asia'
    assert info.session_date == date(2026, 1, 15)
    assert info.folded is True


def test_late_session_fold_crosses_month_and_year_end():
    info = T.classify_session(utc(2026, 12, 31, 23, 59, 59))
    assert (info.session, info.session_date) == ('asia', date(2027, 1, 1))


def test_late_session_exactly_at_2100_folds_in_both_dst_regimes():
    # US cash closes at or before 21:00 in both regimes ([open, close) is half-open),
    # so 21:00 sharp is late and folds.
    for day in (WINTER_WED, SUMMER_WED):
        info = T.classify_session(utc(*day, 21, 0))
        assert info.folded is True
        assert info.session_date == date(day[0], day[1], day[2]) + timedelta(days=1)


def test_late_session_stays_us_when_us_cash_is_open(monkeypatch):
    # With the spec's hours the US cash session never covers 21:00-24:00 UTC, so
    # the "unless US cash is open" branch cannot trigger on real clock times.
    # Force it to prove the branch is implemented.
    monkeypatch.setattr(T, 'is_us_cash_open', lambda instant: True)
    info = T.classify_session(utc(2026, 1, 14, 22, 0))
    assert info.bucket == 'late'
    assert info.session == 'us'
    assert info.session_date == date(2026, 1, 14)
    assert info.folded is False


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        T.classify_session(datetime(2026, 1, 14, 10, 0))


def test_non_utc_aware_input_is_converted():
    plus3 = timezone(timedelta(hours=3))
    info = T.classify_session(datetime(2026, 1, 14, 10, 0, tzinfo=plus3))  # 07:00 UTC
    assert info.bucket == 'europe'


# --- weekend --------------------------------------------------------------
# 2026-01-16 is a Friday, 2026-01-18 a Sunday.
def test_weekend_starts_friday_2100_inclusive():
    assert T.is_weekend(utc(2026, 1, 16, 20, 59, 59, 999999)) is False
    assert T.is_weekend(utc(2026, 1, 16, 21, 0)) is True


def test_weekend_ends_sunday_2100_exclusive():
    assert T.is_weekend(utc(2026, 1, 18, 20, 59, 59, 999999)) is True
    assert T.is_weekend(utc(2026, 1, 18, 21, 0)) is False


def test_weekend_covers_saturday_and_excludes_midweek():
    assert T.is_weekend(utc(2026, 1, 17, 12, 0)) is True
    assert T.is_weekend(utc(2026, 1, 14, 12, 0)) is False
    assert T.is_weekend(utc(2026, 1, 19, 0, 0)) is False  # Monday


# --- US cash session and DST ---------------------------------------------
def test_us_dst_detected_from_zoneinfo():
    assert T.is_us_dst(utc(*SUMMER_WED, 12)) is True
    assert T.is_us_dst(utc(*WINTER_WED, 12)) is False


def test_us_cash_window_shifts_with_daylight_time():
    summer = T.us_cash_window_utc(date(*SUMMER_WED))
    winter = T.us_cash_window_utc(date(*WINTER_WED))
    assert summer == (utc(*SUMMER_WED, 13, 30), utc(*SUMMER_WED, 20, 0))
    assert winter == (utc(*WINTER_WED, 14, 30), utc(*WINTER_WED, 21, 0))


def test_us_cash_boundaries_inside_daylight_time():
    assert T.is_us_cash_open(utc(*SUMMER_WED, 13, 29, 59)) is False
    assert T.is_us_cash_open(utc(*SUMMER_WED, 13, 30)) is True
    assert T.is_us_cash_open(utc(*SUMMER_WED, 19, 59, 59)) is True
    assert T.is_us_cash_open(utc(*SUMMER_WED, 20, 0)) is False


def test_us_cash_boundaries_outside_daylight_time():
    assert T.is_us_cash_open(utc(*WINTER_WED, 13, 30)) is False  # open in summer, closed in winter
    assert T.is_us_cash_open(utc(*WINTER_WED, 14, 29, 59)) is False
    assert T.is_us_cash_open(utc(*WINTER_WED, 14, 30)) is True
    assert T.is_us_cash_open(utc(*WINTER_WED, 20, 0)) is True  # closed in summer, open in winter
    assert T.is_us_cash_open(utc(*WINTER_WED, 20, 59, 59)) is True
    assert T.is_us_cash_open(utc(*WINTER_WED, 21, 0)) is False


def test_us_cash_shift_across_the_march_2026_transition():
    # US DST began Sunday 2026-03-08. Friday the 6th is standard, Monday the 9th is daylight.
    assert T.is_us_cash_open(utc(2026, 3, 6, 13, 45)) is False
    assert T.is_us_cash_open(utc(2026, 3, 9, 13, 45)) is True


def test_us_session_with_open_cash_is_us_not_late_before_2100():
    info = T.classify_session(utc(*WINTER_WED, 20, 30))
    assert (info.bucket, info.session, info.folded) == ('us', 'us', False)


# --- horizons -------------------------------------------------------------
@pytest.mark.parametrize('horizon,hours', [('scalp', 4), ('swing', 24), ('position', 72)])
def test_vertical_barrier_deadline(horizon, hours):
    entry = utc(2026, 1, 14, 10, 0)
    assert T.vertical_barrier_deadline(entry, horizon) == entry + timedelta(hours=hours)
    assert T.horizon_hours(horizon) == C.VERTICAL_BARRIER_HOURS[horizon]


def test_unknown_horizon_raises():
    with pytest.raises(ValueError):
        T.vertical_barrier_deadline(utc(2026, 1, 14), 'forever')


# --- event windows --------------------------------------------------------
def test_cpi_window_is_30_minutes_each_side_closed():
    cpi = utc(2026, 1, 14, 13, 30)
    events = [('cpi', cpi)]
    assert T.in_event_window(cpi - timedelta(minutes=31), events) is False
    assert T.in_event_window(cpi - timedelta(minutes=30), events) is True
    assert T.in_event_window(cpi + timedelta(minutes=30), events) is True
    assert T.in_event_window(cpi + timedelta(minutes=31), events) is False


def test_fomc_window_matches_cpi_and_expiry_is_15_minutes():
    t = utc(2026, 1, 28, 19, 0)
    assert T.in_event_window(t - timedelta(minutes=30), [('fomc', t)]) is True
    assert T.in_event_window(t - timedelta(minutes=16), [('expiry', t)]) is False
    assert T.in_event_window(t - timedelta(minutes=15), [('expiry', t)]) is True
    assert T.in_event_window(t + timedelta(minutes=15), [('expiry', t)]) is True
    assert T.in_event_window(t + timedelta(minutes=16), [('expiry', t)]) is False


def test_active_event_windows_reports_which_events():
    t = utc(2026, 1, 14, 13, 30)
    events = [('cpi', t), ('expiry', t + timedelta(hours=5))]
    active = T.active_event_windows(t + timedelta(minutes=10), events)
    assert [e.kind for e in active] == ['cpi']


def test_event_window_unknown_kind_and_naive_time_raise():
    with pytest.raises(ValueError):
        T.event_window('earnings', utc(2026, 1, 14))
    with pytest.raises(ValueError):
        T.event_window('cpi', datetime(2026, 1, 14, 13, 30))


def test_no_events_means_no_stand_down():
    assert T.in_event_window(utc(2026, 1, 14), []) is False
