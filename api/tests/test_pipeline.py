"""The live signal pipeline (M10): labels first, then features, rules and options for the newest closed bar (throwaway schema)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models import enums as E
from app.models.decisions import Candidate
from app.worker import feature_job as FJ
from app.worker import jobs
from app.worker import label_job as LJ
from app.worker import options_job as OJ
from app.worker import pipeline as PL
from app.worker import rules_job as RJ
from tests.rules_helpers import T0, add_feature_rows

NOW = T0 + timedelta(minutes=2)  # the newest closed bar is T0
COINS = ('BTCUSDT', 'SOLUSDT')


def seed_rows(session, coins=COINS):
    for coin in coins:
        extra = {} if coin == 'BTCUSDT' else {'alt_residual_z_signal_bar': 1.3}
        add_feature_rows(session, coin, T0, source=E.SOURCE_LIVE, **extra)


def live_candidates(session):
    return session.execute(select(Candidate).where(Candidate.source == E.SOURCE_LIVE).order_by(Candidate.id)).scalars().all()


def test_a_tick_decides_and_checks_the_newest_bar_and_a_second_tick_changes_nothing(session):
    seed_rows(session)
    first = PL.run_signal_pipeline(session, COINS, now=NOW, code_version='testver')
    assert first['bar_close'] == T0.isoformat()
    assert first['features']['written'] == 0 and first['features']['ready'] == 2  # rows were already there
    assert first['rules']['deferred'] is None and first['rules']['created'] >= 2
    rows = live_candidates(session)
    assert rows and all(c.options_verdict is not None for c in rows)
    assert first['options']['checked'] == len(rows)
    assert {c.decision_time for c in rows} == {NOW}

    second = PL.run_signal_pipeline(session, COINS, now=NOW + timedelta(minutes=1), code_version='testver')
    assert second['rules']['created'] == 0 and second['options']['checked'] == 0
    assert len(live_candidates(session)) == len(rows)


def test_nothing_is_decided_until_btc_has_its_rows(session, monkeypatch):
    seed_rows(session, coins=('SOLUSDT',))

    def not_ready(session_, coin, bar_close, **kw):
        raise FJ.InputsNotReady([f'{coin} perp 15m'])

    monkeypatch.setattr(FJ, 'assemble_feature_rows', not_ready)
    summary = PL.run_signal_pipeline(session, COINS, now=NOW, code_version='testver')
    assert summary['features']['not_ready'] == {'BTCUSDT': ['BTCUSDT perp 15m']}
    assert 'BTCUSDT' in summary['rules']['deferred']
    assert session.scalar(select(func.count()).select_from(Candidate)) == 0


def test_only_coins_missing_rows_are_assembled(session, monkeypatch):
    seed_rows(session, coins=('BTCUSDT',))
    assembled: list[str] = []

    def record(session_, coin, bar_close, **kw):
        assembled.append(coin)
        raise FJ.InputsNotReady(['x'])

    monkeypatch.setattr(FJ, 'assemble_feature_rows', record)
    PL.run_signal_pipeline(session, COINS, now=NOW, code_version='testver')
    assert assembled == ['SOLUSDT']


def test_labels_run_before_features_rules_and_options(session, monkeypatch):
    seed_rows(session)
    calls: list[str] = []
    for module, name in ((LJ, 'run_label_job'), (RJ, 'run_rules_for_bar'), (OJ, 'run_options_job')):
        real = getattr(module, name)

        def wrapped(*args, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*args, **kw)

        monkeypatch.setattr(module, name, wrapped)
    PL.run_signal_pipeline(session, COINS, now=NOW, code_version='testver')
    assert calls == ['run_label_job', 'run_rules_for_bar', 'run_options_job']


def fake_detail(**overrides):
    detail = {
        'bar_close': T0.isoformat(),
        'labels': {'labeled': 1, 'no_entry': 0, 'errors': 0},
        'features': {'written': 3, 'not_ready': {}, 'hash_mismatches': 0, 'ready': 2},
        'rules': {'deferred': None, 'created': 1, 'already_present': 0},
        'options': {'checked': 1, 'statuses': {'alert': 1}, 'estimate_errors': 0, 'price_mismatches': 0},
    }
    for key, value in overrides.items():
        detail[key] = {**detail[key], **value}
    return detail


def job_ctx():
    return SimpleNamespace(
        settings=SimpleNamespace(watchlist_symbols=list(COINS)), client=SimpleNamespace(now=lambda: NOW), code_version='testver'
    )


def test_the_job_reports_rows_and_turns_errors_into_a_failed_run(monkeypatch):
    monkeypatch.setattr(PL, 'run_signal_pipeline', lambda *a, **k: fake_detail())
    ok = jobs.job_signal_pipeline(None, job_ctx())  # type: ignore[arg-type]
    assert ok.rows_touched == 6 and not ok.errors and not ok.skipped

    monkeypatch.setattr(PL, 'run_signal_pipeline', lambda *a, **k: fake_detail(options={'estimate_errors': 2}))
    bad = jobs.job_signal_pipeline(None, job_ctx())  # type: ignore[arg-type]
    assert bad.errors and 'estimate error' in bad.errors[0]


def test_an_idle_tick_is_skipped(monkeypatch):
    idle = fake_detail(labels={'labeled': 0}, features={'written': 0}, rules={'created': 0}, options={'checked': 0})
    monkeypatch.setattr(PL, 'run_signal_pipeline', lambda *a, **k: idle)
    assert jobs.job_signal_pipeline(None, job_ctx()).skipped  # type: ignore[arg-type]


def test_the_pipeline_is_a_registered_live_job():
    assert jobs.JOBS[jobs.JOB_SIGNAL_PIPELINE] is jobs.job_signal_pipeline
    assert jobs.JOB_SIGNAL_PIPELINE in jobs.LIVE_JOB_NAMES
