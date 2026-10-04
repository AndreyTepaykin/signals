"""Observation factory for the pure stats tests."""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.stats.observations import BaselineObservation, Observation

UTC = timezone.utc
#: After the thin-lookback cutoff (2026-09-24), so an observation is `full` unless a test says otherwise.
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

_ids = itertools.count(1)


def _values(overrides: dict[str, Any]) -> dict[str, Any]:
    n = next(_ids)
    values: dict[str, Any] = {
        'candidate_id': n,
        'sample_group_id': str(uuid.uuid4()),
        'decision_time': T0 + timedelta(minutes=15 * n),
        'bar_close': T0 + timedelta(minutes=15 * n),
        'resolved_at': T0 + timedelta(minutes=15 * n + 60),
        'coin': 'SOLUSDT',
        'horizon': 'swing',
        'source': 'replay',
        'status': 'alert',
        'status_reason': 'options_ignored:no_options_book',
        'trigger': 'squeeze',
        'setup_family': 'squeeze',
        'trend_family': 'chop',
        'vol_pct': 0.5,
        'session': 'us',
        'event_flag': False,
        'first_barrier': 'upper',
        'stored_r': 1.0,
        'funding_r': 0.0,
        'barrier_ambiguous': False,
        'funding_partial': False,
        'gapped_stop': False,
        'p_upper': None,
        'options_verdict': 'ignore',
        'options_snapshot_time': None,
        'barriers_unmodified': True,
    }
    values.update(overrides)
    return values


def obs(**overrides: Any) -> Observation:
    """A filled, full-lookback, replay, alt (no options book) alert that won +1R. Override anything."""
    return Observation(**_values(overrides))


def baseline_obs(**overrides: Any) -> BaselineObservation:
    """A naive-baseline trade (no options layer, so no verdict or reason). Same defaults as obs."""
    values = {'status_reason': None, 'options_verdict': None}
    values.update(overrides)
    return BaselineObservation(**_values(values))


def no_entry(**overrides: Any) -> Observation:
    """A `no_entry` label: no trade, every R NULL."""
    values: dict[str, Any] = {'first_barrier': 'no_entry', 'stored_r': None, 'funding_r': None}
    values.update(overrides)
    return obs(**values)


def btc_no_history(**overrides: Any) -> Observation:
    """A replay BTC row with no Deribit book (the M7 marker)."""
    values: dict[str, Any] = {
        'coin': 'BTCUSDT',
        'status': 'watch',
        'status_reason': 'options_ignored:no_options_history',
        'options_verdict': 'ignore',
        'options_snapshot_time': None,
    }
    values.update(overrides)
    return obs(**values)


def live_btc_layer(**overrides: Any) -> Observation:
    """A live BTC alert that went through the options layer with a real verdict."""
    values: dict[str, Any] = {
        'coin': 'BTCUSDT',
        'source': 'live',
        'status': 'alert',
        'status_reason': None,
        'options_verdict': 'align',
        'options_snapshot_time': T0,
    }
    values.update(overrides)
    return obs(**values)
