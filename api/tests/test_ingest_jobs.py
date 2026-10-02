"""job_run bookkeeping, the live jobs, the scheduler/worker lifecycle, the force-order runner, and the backfill CLI."""

from __future__ import annotations

import argparse
import asyncio
import re
import threading
import time
from datetime import timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings
from app.models.observability import JobRun
from app.worker import backfill, jobs, scheduler
from app.worker import stream as stream_module
from app.worker.feeds import forceorder as FO
from app.worker.feeds import health
from app.worker.feeds.http import RetryPolicy
from ingest_helpers import NOW, at, exchange, make_settings, session_factory, worker_context  # noqa: F401
from test_ingest_forceorder import Clock, Script, event

UTC = timezone.utc


def job_rows(session, name=None):
    sql = 'select job_name, status, rows_touched, error, detail, source, finished_at, started_at from job_run'
    params = {}
    if name:
        sql += ' where job_name = :n'
        params['n'] = name
    return session.execute(text(sql + ' order by id'), params).all()


# ---------------------------------------------------------------------------
# run_job
# ---------------------------------------------------------------------------
def test_run_job_records_ok_with_rows_touched_and_detail(session, worker_context):
    status = jobs.run_job(worker_context, 'unit', lambda s, c: jobs.JobOutcome(rows_touched=7, detail={'k': 1}))
    (row,) = job_rows(session, 'unit')
    assert status == row.status == 'ok' and row.rows_touched == 7 and row.detail == {'k': 1}
    assert row.source == 'live' and row.finished_at >= row.started_at and row.error is None


def test_run_job_records_skipped_when_nothing_was_due(session, worker_context):
    assert jobs.run_job(worker_context, 'unit', lambda s, c: jobs.JobOutcome(skipped=True)) == 'skipped'
    assert job_rows(session, 'unit')[0].status == 'skipped'


def test_run_job_records_an_exception_and_does_not_raise(session, worker_context):
    def boom(s, c):
        raise RuntimeError('kaboom')

    assert jobs.run_job(worker_context, 'unit', boom) == 'error'
    (row,) = job_rows(session, 'unit')
    assert row.status == 'error' and 'RuntimeError: kaboom' in row.error and row.finished_at is not None


def test_run_job_turns_collected_series_errors_into_an_error_run(session, worker_context):
    status = jobs.run_job(worker_context, 'unit', lambda s, c: jobs.JobOutcome(rows_touched=3, errors=['a: x', 'b: y']))
    (row,) = job_rows(session, 'unit')
    assert status == 'error' and row.error == 'a: x; b: y' and row.rows_touched == 3


def test_the_error_record_survives_the_jobs_own_rollback(session, worker_context):
    def writes_then_fails(s, c):
        s.execute(text("insert into job_run (job_name, started_at, status) values ('inner', now(), 'ok')"))
        raise RuntimeError('after a write')

    jobs.run_job(worker_context, 'unit', writes_then_fails)
    assert job_rows(session, 'unit')[0].status == 'error'  # bookkeeping is a separate transaction


def test_orphaned_running_rows_are_closed_but_backfill_rows_are_left_alone(session, session_factory):
    session.execute(text("insert into job_run (job_name, started_at, status) values ('binance_klines', now(), 'running'), ('backfill', now(), 'running'), ('binance_klines', now(), 'ok')"))
    session.flush()
    assert jobs.reap_orphaned_runs(session_factory) == 1
    rows = {(r.job_name, r.status) for r in job_rows(session)}
    assert ('binance_klines', 'error') in rows and ('backfill', 'running') in rows
    assert 'orphaned' in session.execute(text("select error from job_run where job_name='binance_klines' and status='error'")).scalar_one()


# ---------------------------------------------------------------------------
# the live jobs
# ---------------------------------------------------------------------------
def test_the_klines_job_fills_every_series_and_a_second_run_is_skipped(session, worker_context):
    assert jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines) == 'ok'
    first = job_rows(session, jobs.JOB_KLINES)[0]
    assert first.rows_touched > 0 and first.detail['series_checked'] == 12 and first.detail['series_fetched'] == 12
    assert session.execute(text('select count(distinct (coin, market, timeframe)) from candle')).scalar_one() == 12
    assert session.execute(text('select count(*) from candle where close_time > :n'), {'n': NOW}).scalar_one() == 0
    assert session.execute(text('select count(*) from candle where available_at <> close_time')).scalar_one() == 0

    assert jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines) == 'skipped'
    second = job_rows(session, jobs.JOB_KLINES)[1]
    assert second.status == 'skipped' and second.rows_touched == 0 and second.detail['series_fetched'] == 0


def test_the_next_poll_after_a_bar_closes_fetches_exactly_the_new_bars(session, worker_context, exchange):
    jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines)
    exchange.now = at(17, 0) + timedelta(seconds=30)  # the 16:45 15m bar and the 16:00 1h bar have just closed
    assert jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines) == 'ok'
    latest = job_rows(session, jobs.JOB_KLINES)[-1]
    assert latest.rows_touched == 2 * 2 * 2  # 2 coins x 2 markets x (one 15m + one 1h)
    assert latest.detail['series_fetched'] == 8


def test_a_failing_exchange_makes_the_run_an_error_and_is_remembered_for_health(session, worker_context, exchange):
    exchange.interceptor = lambda request: httpx.Response(500) if request.url.path.endswith('/klines') else None
    worker_context.client.futures.retry = RetryPolicy(max_attempts=2, base_delay=0, jitter=0)
    worker_context.client.spot.retry = RetryPolicy(max_attempts=2, base_delay=0, jitter=0)
    assert jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines) == 'error'
    (row,) = job_rows(session, jobs.JOB_KLINES)
    assert 'BinanceUnavailable' in row.error
    attempt = worker_context.attempts.get('binance_kline_perp_15m', 'BTCUSDT')
    assert attempt and attempt.last_error_at and attempt.last_success_at is None


def test_a_geo_block_aborts_the_job_and_marks_every_kline_series_failed(session, worker_context, exchange):
    exchange.interceptor = lambda request: httpx.Response(451)
    assert jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines) == 'error'
    assert 'GeoBlocked' in job_rows(session, jobs.JOB_KLINES)[0].error
    assert worker_context.attempts.get('binance_kline_spot_4h', 'ETHUSDT').last_error_at is not None


def test_the_open_interest_job_stamps_rows_at_the_poll_close_plus_the_lag(session, worker_context):
    assert jobs.run_job(worker_context, jobs.JOB_OPEN_INTEREST, jobs.job_open_interest) == 'ok'
    assert session.execute(text('select count(*) from open_interest')).scalar_one() > 0
    lag = worker_context.settings.feed_open_interest_publication_lag_seconds
    assert session.execute(text('select count(*) from open_interest where available_at <> poll_time + make_interval(secs => :s)'), {'s': lag}).scalar_one() == 0
    assert session.execute(text('select count(*) from open_interest where available_at = poll_time')).scalar_one() == 0  # none carries the optimistic stamp
    assert session.execute(text('select count(*) from open_interest where available_at > :n'), {'n': NOW}).scalar_one() == 0
    assert jobs.run_job(worker_context, jobs.JOB_OPEN_INTEREST, jobs.job_open_interest) == 'skipped'


def test_the_settled_funding_job_is_idempotent(session, worker_context):
    assert jobs.run_job(worker_context, jobs.JOB_FUNDING_SETTLED, jobs.job_funding_settled) == 'ok'
    count = session.execute(text("select count(*) from funding where kind='settled'")).scalar_one()
    assert count > 0
    assert jobs.run_job(worker_context, jobs.JOB_FUNDING_SETTLED, jobs.job_funding_settled) == 'skipped'
    assert session.execute(text("select count(*) from funding where kind='settled'")).scalar_one() == count


def test_the_predicted_funding_job_reports_coins_the_exchange_did_not_return(session, worker_context):
    worker_context.settings = make_settings(watchlist='BTCUSDT,ETHUSDT,SOLUSDT')
    assert jobs.run_job(worker_context, jobs.JOB_FUNDING_PREDICTED, jobs.job_funding_predicted) == 'ok'
    assert session.execute(text("select count(*) from funding where kind='predicted'")).scalar_one() == 2
    assert worker_context.attempts.get(health.FEED_FUNDING_PREDICTED, 'BTCUSDT').last_success_at is not None
    assert worker_context.attempts.get(health.FEED_FUNDING_PREDICTED, 'SOLUSDT').last_error_at is not None


def test_the_health_job_after_ingest_reports_every_feed(session, worker_context):
    for name in (jobs.JOB_KLINES, jobs.JOB_OPEN_INTEREST, jobs.JOB_FUNDING_SETTLED, jobs.JOB_FUNDING_PREDICTED):
        jobs.run_job(worker_context, name, jobs.JOBS[name])
    assert jobs.run_job(worker_context, jobs.JOB_FEED_HEALTH, jobs.job_feed_health) == 'ok'
    counts = job_rows(session, jobs.JOB_FEED_HEALTH)[0].detail['status_counts']
    assert sum(counts.values()) == 22 and counts.get('ok', 0) >= 20
    assert session.execute(text("select count(*) from feed_health where status='unknown'")).scalar_one() <= 2

def test_job_intervals_sit_inside_the_spec_one_to_five_minutes():
    intervals = jobs.job_intervals(Settings(_env_file=None, watchlist='BTCUSDT'))
    assert set(intervals) == set(jobs.JOBS) and len(intervals) == 5
    assert all(60 <= seconds <= 300 for seconds in intervals.values())


# ---------------------------------------------------------------------------
# scheduler & worker
# ---------------------------------------------------------------------------
def test_every_job_is_registered_once_and_cannot_overlap_itself(worker_context):
    sched = scheduler.build_scheduler(worker_context, start_at=NOW + timedelta(days=1))
    sched.start(paused=True)  # job defaults are applied when jobs are really added; paused: nothing runs
    try:
        registered = {job.id: job for job in sched.get_jobs()}
    finally:
        sched.shutdown(wait=False)
    assert set(registered) == set(jobs.JOBS)
    expected = jobs.job_intervals(worker_context.settings)
    for name, job in registered.items():
        assert job.max_instances == 1 and job.coalesce is True
        assert job.trigger.interval == timedelta(seconds=expected[name])
    starts = sorted(job.next_run_time for job in registered.values())
    assert len(set(starts)) == 5  # staggered, not a thundering herd


@pytest.fixture
def quiet_worker(monkeypatch, worker_context):
    """A Worker whose scheduled jobs never fire during the test (they would share the test's one connection)."""
    original = scheduler.build_scheduler
    monkeypatch.setattr(scheduler, 'build_scheduler', lambda ctx: original(ctx, start_at=NOW + timedelta(days=1)))
    monkeypatch.setattr(scheduler, 'dispose_sync_engine', lambda: None)
    return scheduler.Worker(worker_context, with_stream=False)


def test_the_worker_starts_reaps_orphans_and_stops_cleanly(session, quiet_worker, worker_context):
    session.execute(text("insert into job_run (job_name, started_at, status) values ('binance_klines', now(), 'running')"))
    session.flush()
    quiet_worker.start()
    assert quiet_worker.scheduler.running
    quiet_worker.stop()
    assert not quiet_worker.scheduler.running and worker_context.stopping.is_set()
    statuses = {r.status for r in job_rows(session, 'binance_klines')}
    assert statuses == {'error'}  # the orphan was closed at start-up
    assert len(job_rows(session, 'feed_health')) == 1  # the final snapshot on the way out
    quiet_worker.stop()  # a second stop is a no-op, not an error


def test_run_once_runs_each_job_without_the_stream(session, quiet_worker):
    results = quiet_worker.run_once()
    assert set(results) == set(jobs.JOBS) and set(results.values()) <= {'ok', 'skipped'}
    assert {r.job_name for r in job_rows(session)} == set(jobs.JOBS)


def test_the_stop_flag_interrupts_a_job_between_series(session, worker_context):
    worker_context.stopping.set()
    jobs.run_job(worker_context, jobs.JOB_KLINES, jobs.job_klines)
    row = job_rows(session, jobs.JOB_KLINES)[0]
    assert row.detail['interrupted'] is True and row.rows_touched == 0


# ---------------------------------------------------------------------------
# the force-order runner against the database
# ---------------------------------------------------------------------------
def test_the_stream_runner_writes_rows_a_job_run_per_connection_and_feed_health(session, worker_context):
    stop_placeholder = asyncio.Event()
    script = Script(Clock(), stop_placeholder, [event(), event(t=1790958600124), event('DOGEUSDT')])
    runner = stream_module.ForceOrderRunner(worker_context)
    runner.collector = FO.ForceOrderCollector(
        base_url='wss://example.test',
        watchlist=frozenset(worker_context.settings.watchlist_symbols),
        sink=runner._sink,
        connector=script,
        hooks=stream_module.DatabaseHooks(worker_context),
        flush_interval=0.05,
    )
    runner.start()
    try:
        deadline = time.monotonic() + 8
        while runner.collector.stats.written < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        runner.stop()
    assert runner.collector.stats.written == 2
    assert session.execute(text('select count(*) from force_order')).scalar_one() == 2
    assert session.execute(text('select count(*) from force_order where available_at <> traded_at')).scalar_one() == 0
    (run,) = job_rows(session, jobs.JOB_FORCE_ORDER_STREAM)
    assert run.status == 'ok' and run.rows_touched == 2 and run.detail['partial_stream'] is True and run.detail['ignored'] == 1
    status, detail = session.execute(text("select status, detail from feed_health where feed = 'binance_force_order'")).one()
    assert status == 'down'  # stopped: not connected any more
    assert 'ESTIMATE' in detail


# ---------------------------------------------------------------------------
# the backfill CLI
# ---------------------------------------------------------------------------
@pytest.fixture
def cli(monkeypatch, session_factory, exchange):  # noqa: F811
    monkeypatch.setattr(backfill, 'get_settings', lambda: make_settings())
    monkeypatch.setattr(backfill, 'get_sync_session_factory', lambda: session_factory)
    monkeypatch.setattr(backfill, 'dispose_sync_engine', lambda: None)
    monkeypatch.setattr(backfill, 'BinanceClient', lambda settings=None: exchange.client())
    return exchange


def new_rows(output: str, feed: str) -> int:
    block = output.split(feed, 1)[1]
    return int(re.search(r'NEW (\d+)', block).group(1))


def test_backfill_one_day_of_btc_15m_prints_a_summary_and_records_the_run(cli, session, capsys):
    code = backfill.main(['--coins', 'BTCUSDT', '--timeframes', '15m', '--days', '1'])
    out = capsys.readouterr().out
    assert code == 0
    for feed in ('kline:perp:15m', 'kline:spot:15m', 'funding:settled', 'open_interest:15m'):
        assert feed in out
    perp_new = new_rows(out, 'kline:perp:15m')
    assert 94 <= perp_new <= 96
    assert session.execute(text("select count(*) from candle where market='perp'")).scalar_one() == perp_new
    assert session.execute(text('select count(*) from candle where close_time > :n'), {'n': NOW}).scalar_one() == 0
    assert 'gaps       leading 0, interior 0, trailing 0' in out
    (run,) = job_rows(session, 'backfill')
    assert run.status == 'ok' and run.source is None and run.rows_touched > perp_new
    assert run.detail['feeds']['kline:perp:15m']['written'] == perp_new


def test_running_the_same_backfill_again_writes_nothing(cli, session, capsys):
    argv = ['--coins', 'BTCUSDT', '--timeframes', '15m,1h', '--days', '2']
    assert backfill.main(argv) == 0
    capsys.readouterr()
    before = session.execute(text('select (select count(*) from candle), (select count(*) from funding), (select count(*) from open_interest)')).one()
    kline_requests = len(cli.kline_requests())
    assert backfill.main(argv) == 0
    out = capsys.readouterr().out
    assert new_rows(out, 'kline:perp:15m') == 0 and new_rows(out, 'funding:settled') == 0
    after = session.execute(text('select (select count(*) from candle), (select count(*) from funding), (select count(*) from open_interest)')).one()
    assert tuple(before) == tuple(after)
    assert len(cli.kline_requests()) == kline_requests  # resumed: not one kline refetched


def test_dry_run_changes_nothing_and_leaves_no_job_run(cli, session, capsys):
    assert backfill.main(['--coins', 'BTCUSDT', '--timeframes', '15m', '--days', '1', '--dry-run', '--feeds', 'klines']) == 0
    out = capsys.readouterr().out
    assert 'DRY RUN' in out and 'missing before this run 9' in out
    assert session.execute(text('select count(*) from candle')).scalar_one() == 0
    assert job_rows(session, 'backfill') == []


def test_backfill_notes_when_open_interest_history_is_older_than_binance_serves(cli, capsys):
    backfill.main(['--coins', 'BTCUSDT', '--timeframes', '4h', '--feeds', 'oi', '--days', '90'])
    out = capsys.readouterr().out
    assert 'only served for the last ~30 days' in out and 'cannot be backfilled' in out


def test_a_geo_block_exits_3_and_says_the_run_is_incomplete(cli, session, capsys):
    cli.interceptor = lambda request: httpx.Response(451)
    assert backfill.main(['--coins', 'BTCUSDT', '--timeframes', '15m', '--days', '1']) == 3
    captured = capsys.readouterr()
    assert 'GeoBlocked' in captured.err and 'cut short' in captured.out
    (run,) = job_rows(session, 'backfill')
    assert run.status == 'error' and 'GeoBlocked' in run.error


def test_one_failed_series_exits_2_but_the_other_feeds_still_run(cli, session, capsys):
    cli.interceptor = lambda request: httpx.Response(500) if request.url.path == '/fapi/v1/fundingRate' else None
    assert backfill.main(['--coins', 'BTCUSDT', '--timeframes', '15m', '--days', '1', '--feeds', 'klines,funding']) == 2
    out = capsys.readouterr().out
    assert 'FAILED for 1 of 1' in out and 'ERROR' in out
    assert session.execute(text('select count(*) from candle')).scalar_one() > 0


@pytest.mark.parametrize(
    'argv, message',
    [
        (['--coins', 'BTCUSDT'], 'give --start or --days'),
        (['--days', '3', '--start', '2026-09-01'], 'not both'),
        (['--days', '0'], 'must be positive'),
        (['--start', '2026-10-05', '--end', '2026-10-01'], 'is not before end'),
        (['--days', '1', '--timeframes', '5m'], 'unknown timeframe'),
        (['--days', '1', '--markets', 'swap'], 'unknown market'),
        (['--days', '1', '--feeds', 'liquidations'], 'unknown feed'),
        (['--days', '1', '--coins', 'BTCUSD'], 'error:'),
    ],
)
def test_bad_arguments_stop_before_any_request(cli, argv, message):
    with pytest.raises(SystemExit) as stopped:
        backfill.main(argv)
    assert message in str(stopped.value.code)
    assert cli.requests_to('/fapi/v1/klines') == []


def test_unknown_flags_are_an_argparse_error(cli):
    with pytest.raises(SystemExit) as stopped:
        backfill.main(['--days', '1', '--fetch-orders'])
    assert stopped.value.code == 2


def test_parse_instant_reads_a_naive_value_as_utc_and_converts_offsets():
    assert backfill.parse_instant('2026-10-01') == at(0, 0, day=1)
    assert backfill.parse_instant('2026-10-01T12:30').tzinfo is not None
    assert backfill.parse_instant('2026-10-01T12:30') == at(12, 30, day=1)
    assert backfill.parse_instant('2026-10-01T15:30+03:00') == at(12, 30, day=1)
    assert backfill.parse_instant('2026-10-01T15:30+03:00').utcoffset() == timedelta(0)
    with pytest.raises(argparse.ArgumentTypeError):
        backfill.parse_instant('yesterday')


def test_the_timeframes_choice_also_drives_open_interest_periods(cli, session, capsys):
    backfill.main(['--coins', 'BTCUSDT', '--timeframes', '1h', '--days', '2', '--feeds', 'oi'])
    assert session.execute(text("select distinct timeframe from open_interest")).scalars().all() == ['1h']
