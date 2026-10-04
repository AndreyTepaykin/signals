"""The labeler semantics are part of the ``rule_hash``: every ``LABEL_*`` constant is hashed and no param hides outside it."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core import constants as C
from app.core import hashing as H
from app.core.labeler import barriers as B

API = Path(__file__).resolve().parents[1]
LABELER_MODULES = sorted((API / 'app' / 'core' / 'labeler').glob('*.py')) + [API / 'app' / 'worker' / 'label_job.py']
LABEL_CONSTANTS = sorted(name for name in dir(C) if name.startswith('LABEL_'))

# storage scale and unit conversions (not semantics), the sign table, and a report-only threshold
UNHASHED_ALLOWLIST = {'R_QUANTUM', 'HOURS_QUANTUM', 'BPS_QUANTUM', '_BPS_PER_UNIT', '_SECONDS_PER_HOUR', 'SIDE_SIGN', 'MISSING_BAR_AFTER'}
EXPECTED_LABEL_CONSTANTS = {
    'LABEL_AMBIGUOUS_BAR_RESOLVES_TO': 'stop',
    'LABEL_GAP_STOP_FILLS_AT': 'open',
    'LABEL_GAP_TARGET_FILLS_AT': 'barrier',
    'LABEL_FEES_CHARGED_ON': 'entry_price',
    'LABEL_VERTICAL_EXIT': 'deadline_bar_close',
    'LABEL_RESOLVED_AT': 'bar_close',
    'LABEL_NO_ENTRY_RULE': 'open_at_or_through_stop_or_through_cap',
    'LABEL_FUNDING_WINDOW': 'after_fill_before_exit',
    'LABEL_FUNDING_NOTIONAL': 'mark_price_else_entry',
    'LABEL_FUNDING_MAX_INTERVAL_HOURS': 8,
    'LABEL_FUNDING_GAP_SLACK_MINUTES': 5,
    'LABEL_FUNDING_STALL_CAP_HOURS': 24,
}


def test_the_label_constants_are_the_decided_ones():
    assert {name: getattr(C, name) for name in LABEL_CONSTANTS} == EXPECTED_LABEL_CONSTANTS


def test_every_label_constant_is_in_the_hashed_constants():
    frozen = H.frozen_constants()
    for name in LABEL_CONSTANTS:
        assert frozen[name] == getattr(C, name), name


@pytest.mark.parametrize('name', LABEL_CONSTANTS)
def test_changing_a_label_constant_changes_the_rule_hash(name):
    frozen = H.frozen_constants()
    value = frozen[name]
    changed = value + 1 if isinstance(value, int) else value + '_changed'
    assert H.rule_hash(constants={**frozen, name: changed}) != H.rule_hash()


def test_the_rule_version_is_the_one_that_introduced_the_replay_options_marker():
    assert C.RULE_VERSION == 'phase1-v6'


def test_the_derived_labeler_params_follow_the_hashed_constants():
    assert B.FUNDING_MAX_GAP.total_seconds() == (C.LABEL_FUNDING_MAX_INTERVAL_HOURS * 60 + C.LABEL_FUNDING_GAP_SLACK_MINUTES) * 60
    assert B.FUNDING_STALL_CAP.total_seconds() == C.LABEL_FUNDING_STALL_CAP_HOURS * 3600


def _assigned_names(tree: ast.Module):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and node.value is not None:
                yield target.id, node.value


def _is_a_tunable(value: ast.AST) -> bool:
    """A number, Decimal or timedelta literal that no hashed module supplies."""
    names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    if names & {'C', 'P'}:  # derived from the hashed constants or engine params
        return False
    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'Decimal', 'timedelta'}:
            return True
    return False


def test_no_labeler_module_hides_a_numeric_param_outside_the_hash():
    offenders = []
    for path in LABELER_MODULES:
        for name, value in _assigned_names(ast.parse(path.read_text(encoding='utf-8'))):
            if name.lstrip('_').isupper() and name not in UNHASHED_ALLOWLIST and _is_a_tunable(value):
                offenders.append(f'{path.name}:{name}')
    assert offenders == [], f'move into constants.py (hashed) or allowlist deliberately: {offenders}'


def test_the_guard_flags_an_unlisted_param():
    tree = ast.parse('NEW_TOLERANCE = Decimal("0.5")\nOK = C.X * 2\nWORD = "a"\nFLAG = True\nHOURS = 4\n')
    flagged = [name for name, value in _assigned_names(tree) if _is_a_tunable(value)]
    assert flagged == ['NEW_TOLERANCE', 'HOURS']


def test_the_labeler_reads_no_runtime_setting():
    for path in LABELER_MODULES:
        source = path.read_text(encoding='utf-8')
        assert 'app.config' not in source and 'get_settings' not in source, path.name


def test_the_fee_is_the_hashed_round_trip_cost(monkeypatch):
    barriers = B.plan_barriers(
        direction='long', entry=Decimal('100'), stop=Decimal('98'), cap=None, fill_time=datetime(2026, 3, 2, 14, tzinfo=timezone.utc), horizon='scalp'
    )
    assert 'ROUND_TRIP_COST_BPS' in H.frozen_constants()
    assert B.fees_r(barriers) == Decimal('0.06')
    monkeypatch.setattr(C, 'ROUND_TRIP_COST_BPS', 24)
    assert B.fees_r(barriers) == Decimal('0.12')