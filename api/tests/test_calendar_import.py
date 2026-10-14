"""The owner calendar importer and the unlock coverage check (app.worker.calendar_import)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.as_of import as_of
from app.models.feeds import EventCalendar
from app.worker import calendar_import as CI
from app.worker import stats_job
from app.worker.feature_job import read_calendar

UTC = timezone.utc
NOW = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)
HEADER = ','.join(CI.CSV_COLUMNS)


def csv_text(*lines: str) -> str:
    return '\n'.join((HEADER, *lines)) + '\n'


UNLOCK = 'unlock,SOLUSDT,2026-12-01T15:00:00+03:00,,50000000,SOL cliff,token docs'
CPI = 'cpi,,2026-11-12T13:30:00+00:00,,,US CPI,bls.gov'


def rows(*lines: str) -> list[CI.CalendarRow]:
    parsed = CI.parse_csv(csv_text(*lines))
    assert parsed.errors == []
    return parsed.rows


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------
def test_a_valid_unlock_row_parses_and_is_stored_in_utc():
    (row,) = rows(UNLOCK)
    assert row.line == 2
    assert (row.kind, row.coin) == ('unlock', 'SOLUSDT')
    assert row.event_time == datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    assert row.event_time.utcoffset() == timedelta(0)
    assert row.is_large is None
    assert row.notional_usd == Decimal('50000000')
    assert (row.title, row.feed_source) == ('SOL cliff', 'token docs')


def test_a_macro_row_has_no_coin_and_booleans_parse():
    (cpi, expiry) = rows(CPI, 'expiry,BTCUSDT,2026-12-25T08:00:00Z,TRUE,,,deribit')
    assert cpi.coin is None and cpi.title == 'US CPI'
    assert expiry.is_large is True and expiry.title is None


def test_every_error_is_reported_with_its_line_number():
    parsed = CI.parse_csv(
        csv_text(
            'halving,SOLUSDT,2026-12-01T12:00:00+00:00,,,,src',  # bad kind
            'unlock,SOL-USD,2026-12-01T12:00:00+00:00,,,,src',  # bad symbol
            'unlock,SOLUSDT,2026-12-01T12:00:00,,,,src',  # naive
            'fomc,BTCUSDT,2026-12-09T19:00:00+00:00,,,,src',  # coin on a macro row
            'unlock,,2026-12-01T12:00:00+00:00,,,,src',  # unlock without coin
            'unlock,SOLUSDT,2026-12-01T12:00:00+00:00,maybe,-5,,',  # bad bool, negative, no source
            'unlock,SOLUSDT,not a date,,,,src',
        )
    )
    assert parsed.rows == []
    text = '\n'.join(parsed.errors)
    assert "line 2: kind 'halving'" in text
    assert 'line 3: coin' in text and 'SOL-USD' in text
    assert 'line 4: event_time' in text and 'no UTC offset' in text
    assert 'line 5: fomc is a macro event, so coin must be empty' in text
    assert 'line 6: unlock needs a coin' in text
    assert 'line 7: is_large' in text
    assert 'line 7: notional_usd' in text
    assert 'line 7: feed_source is required' in text
    assert 'line 8: event_time' in text


def test_header_problems_stop_before_any_row():
    assert CI.parse_csv('').errors[0].startswith('line 1: the file is empty')
    parsed = CI.parse_csv('kind,coin,event_time,source\nunlock,SOLUSDT,2026-12-01T12:00:00+00:00,x\n')
    assert parsed.rows == []
    assert any('missing' in e for e in parsed.errors)
    assert any('unknown columns source' in e for e in parsed.errors)


def test_field_count_and_duplicate_keys_are_errors():
    parsed = CI.parse_csv(
        csv_text(
            UNLOCK,
            'unlock,SOLUSDT,2026-12-01T12:00:00+00:00,,,,other source',  # same instant as line 2
            'unlock,SOLUSDT,2026-12-02T12:00:00+00:00,,,,src,extra',
            'unlock,SOLUSDT',
        )
    )
    assert parsed.errors == [
        'line 3: same kind, coin and event_time as line 2',
        'line 4: more fields than header columns',
        'line 5: fewer fields than header columns',
    ]


def test_header_only_file_parses_to_nothing():
    parsed = CI.parse_csv(HEADER + '\n')
    assert parsed.rows == [] and parsed.errors == []


def test_the_shipped_template_is_header_only():
    template = Path(__file__).resolve().parents[1] / 'data' / 'calendar' / 'events_template.csv'
    parsed = CI.parse_csv(template.read_text(encoding='utf-8-sig'))
    assert parsed.rows == [] and parsed.errors == []


def test_stand_down_kinds_are_refused_without_the_flag():
    parsed = rows(UNLOCK, CPI, 'fomc,,2026-12-09T19:00:00+00:00,,,,fed', 'expiry,BTCUSDT,2026-12-25T08:00:00+00:00,,,,deribit')
    refusals = CI.stand_down_refusals(parsed, allowed=False)
    assert [r.split(':')[0] for r in refusals] == ['line 3', 'line 4', 'line 5']
    assert CI.stand_down_refusals(parsed, allowed=True) == []
    assert CI.row_notes(parsed) == ['line 5: expiry with empty is_large is treated as large (stands the book down)']


def test_the_cli_refuses_stand_down_kinds_and_writes_nothing(tmp_path, capsys):
    path = tmp_path / 'events.csv'
    path.write_text(csv_text(UNLOCK, CPI), encoding='utf-8')
    assert CI.main(['--file', str(path), '--dry-run']) == 1
    err = capsys.readouterr().err
    assert 'line 3: cpi is a stand-down kind; refused without --allow-stand-down-kinds' in err
    assert 'stand_down:event_window' in err
    assert 'nothing written' in err


def test_the_cli_reports_every_parse_error_and_exits_non_zero(tmp_path, capsys):
    path = tmp_path / 'events.csv'
    path.write_text(csv_text('unlock,SOLUSDT,2026-12-01T12:00:00,,,,src', 'cpi,BTCUSDT,2026-11-12T13:30:00+00:00,,,,bls'), encoding='utf-8')
    assert CI.main(['--file', str(path)]) == 1
    err = capsys.readouterr().err
    assert 'line 2:' in err and 'line 3:' in err
    assert '2 error(s); nothing written.' in err


def test_available_at_default_and_override():
    assert CI.resolve_available_at(None, NOW) == NOW
    earlier = CI.resolve_available_at('2026-10-01T03:00:00+03:00', NOW)
    assert earlier == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match='in the future'):
        CI.resolve_available_at('2026-10-15T00:00:00+00:00', NOW)
    with pytest.raises(ValueError, match='no UTC offset'):
        CI.resolve_available_at('2026-10-01T00:00:00', NOW)


# ---------------------------------------------------------------------------
# Database (throwaway schema, rolled back)
# ---------------------------------------------------------------------------
def stored(session) -> list[EventCalendar]:
    statement = as_of(CI.EVERY_STORED_ROW).select(EventCalendar).order_by(EventCalendar.event_time)
    return list(session.execute(statement).scalars().all())


def test_import_is_idempotent_and_never_changes_a_stored_row(session):
    first = CI.import_rows(session, rows(UNLOCK, CPI), available_at=NOW)
    assert (first.inserted, first.already_present) == (2, 0)
    changed = 'unlock,SOLUSDT,2026-12-01T12:00:00+00:00,true,1,new title,other source'
    second = CI.import_rows(session, rows(changed, CPI), available_at=NOW + timedelta(hours=1))
    assert (second.inserted, second.already_present) == (0, 2)
    cpi, unlock = stored(session)
    assert cpi.coin is None and cpi.feed_source == 'bls.gov'
    assert (unlock.title, unlock.feed_source, unlock.is_large, unlock.notional_usd) == ('SOL cliff', 'token docs', None, Decimal('50000000'))
    assert unlock.available_at == NOW


def test_dry_run_counts_inside_a_read_only_transaction_and_writes_nothing(session, db_engine, db_schema):
    with stats_job.readonly_session(db_engine, search_path=db_schema) as ro:
        report = CI.import_rows(ro, rows(UNLOCK, CPI), available_at=NOW, dry_run=True)
    assert (report.inserted, report.already_present, report.dry_run) == (2, 0, True)
    assert stored(session) == []

    CI.import_rows(session, rows(UNLOCK), available_at=NOW)
    again = CI.import_rows(session, rows(UNLOCK, CPI), available_at=NOW, dry_run=True)
    assert (again.inserted, again.already_present) == (1, 1)
    assert len(stored(session)) == 1


def test_available_at_is_the_knowledge_time_the_gate_reads(session):
    CI.import_rows(session, rows(UNLOCK), available_at=NOW)
    assert CI.count_visible_rows(session, NOW - timedelta(seconds=1)) == 0
    assert CI.count_visible_rows(session, NOW) == 1

    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    CI.import_rows(session, rows('unlock,AVAXUSDT,2026-12-05T00:00:00+00:00,,,,src'), available_at=earlier)
    (avax,) = [r for r in stored(session) if r.coin == 'AVAXUSDT']
    assert avax.available_at == earlier


def test_calendar_has_rows_sees_rows_whatever_their_available_at(session):
    assert CI.calendar_has_rows(session) is False
    CI.import_rows(session, rows(UNLOCK), available_at=NOW)
    assert CI.calendar_has_rows(session) is True


def test_only_a_cpi_or_fomc_row_makes_the_calendar_loaded(session):
    bar = NOW + timedelta(hours=1)
    assert read_calendar(session, bar) == ([], False)
    CI.import_rows(session, rows(UNLOCK), available_at=NOW)
    assert read_calendar(session, bar) == ([], False)  # an unlock alone leaves event_flag unknown
    assert CI.would_flip_event_flag(session, rows(UNLOCK)) is False
    assert CI.would_flip_event_flag(session, rows(CPI)) is True
    CI.import_rows(session, rows(CPI), available_at=NOW)
    events, loaded = read_calendar(session, bar)
    assert loaded is True and events == []
    assert read_calendar(session, NOW - timedelta(minutes=15)) == ([], False)
    assert CI.would_flip_event_flag(session, rows('fomc,,2026-12-09T19:00:00+00:00,,,FOMC,fed')) is False


def test_read_unlocks_is_gated_and_skips_other_kinds(session):
    CI.import_rows(session, rows(UNLOCK, CPI), available_at=NOW)
    assert CI.read_unlocks(session, NOW) == [('SOLUSDT', datetime(2026, 12, 1, 12, 0, tzinfo=UTC))]
    assert CI.read_unlocks(session, NOW - timedelta(seconds=1)) == []
    assert CI.count_visible_rows(session, NOW) == 2


# ---------------------------------------------------------------------------
# Coverage (pure)
# ---------------------------------------------------------------------------
def test_unlock_coverage_counts_rows_and_finds_the_next_unlock_in_the_window():
    watchlist = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'DOTUSDT')
    unlocks = [
        ('SOLUSDT', NOW + timedelta(days=40)),
        ('SOLUSDT', NOW + timedelta(days=10)),
        ('SOLUSDT', NOW - timedelta(days=5)),
        ('AVAXUSDT', NOW + timedelta(days=200)),
        ('BTCUSDT', NOW + timedelta(days=1)),
        ('NOTLISTEDUSDT', NOW + timedelta(days=1)),
    ]
    coverage = CI.unlock_coverage(watchlist, unlocks, now=NOW, days=90)
    assert [c.coin for c in coverage] == ['SOLUSDT', 'AVAXUSDT', 'DOTUSDT']
    sol, avax, dot = coverage
    assert (sol.unlock_rows, sol.next_unlock, sol.has_any) == (3, NOW + timedelta(days=10), True)
    assert (avax.unlock_rows, avax.next_unlock, avax.has_any) == (1, None, True)
    assert (dot.unlock_rows, dot.next_unlock, dot.has_any) == (0, None, False)
    assert CI.missing_unlock_calendar(coverage) == ['DOTUSDT']
    assert CI.unlock_coverage(watchlist, unlocks, now=NOW, days=365)[1].next_unlock == NOW + timedelta(days=200)


def test_format_coverage_prints_the_missing_list():
    coverage = CI.unlock_coverage(('BTCUSDT', 'SOLUSDT', 'DOTUSDT'), [('SOLUSDT', NOW + timedelta(days=3))], now=NOW, days=90)
    text = CI.format_coverage(coverage, now=NOW, days=90, visible_rows=0)
    assert 'missing unlock calendar (1): DOTUSDT' in text
    assert 'NULL "unknown"' in text
    assert 'BTCUSDT' not in text.split('\n', 3)[-1]
    full = CI.format_coverage(coverage[:1], now=NOW, days=90, visible_rows=1)
    assert 'missing unlock calendar: none' in full
