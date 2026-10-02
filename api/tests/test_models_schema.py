"""Schema-wide invariants, asserted straight off the metadata.

These need no database, so they run even when Postgres is down, and they are the
tests that catch a later milestone quietly breaking a project-wide rule.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from sqlalchemy import Boolean, Date, DateTime, Float, Numeric, SmallInteger, Text

from app.models import enums as E
from app.models.as_of import FEED_MODELS
from app.models.base import Base, FeedRow
from app.models.decisions import Candidate
from app.models.features import SHADOW_METRIC_KEYS, FeatureRow
from app.models.labels import LABEL_ONLY_COLUMNS, MFE_DIAGNOSTIC_COLUMNS, Label

METADATA = Base.metadata

EXPECTED_TABLES = {
    'candle',
    'open_interest',
    'funding',
    'force_order',
    'event_calendar',
    'feed_health',
    'options_snapshot',
    'options_metric',
    'feature_row',
    'candidate',
    'label',
    'job_run',
    'promotion_log',
}


def test_the_table_list_is_exactly_what_milestone_one_owes():
    assert set(METADATA.tables) == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# Rule 3: no order, position or execution table, in any phase
# ---------------------------------------------------------------------------
FORBIDDEN_TABLE_NAMES = {
    'order',
    'orders',
    'order_request',
    'position',
    'positions',
    'execution',
    'executions',
    'fill',
    'fills',
    'trade',
    'trades',
}


def test_no_order_position_or_execution_table():
    assert set(METADATA.tables).isdisjoint(FORBIDDEN_TABLE_NAMES)


def test_no_table_name_suggests_sending_an_order():
    for name in METADATA.tables:
        assert 'order' not in name or name == 'force_order', (
            f'{name!r}: the only table that may mention an order is force_order, '
            'which is a liquidation feed named by the venue. Nothing sends orders.'
        )


# ---------------------------------------------------------------------------
# Rule: exact numerics and UTC timestamps everywhere
# ---------------------------------------------------------------------------
def test_no_floating_point_column_anywhere():
    offenders = [
        f'{table.name}.{column.name}'
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


def test_every_timestamp_is_timezone_aware():
    offenders = [
        f'{table.name}.{column.name}'
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert offenders == []


def test_the_only_date_column_is_a_session_date():
    date_columns = [
        f'{table.name}.{column.name}'
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, Date) and not isinstance(column.type, DateTime)
    ]
    assert date_columns == ['feature_row.session_date']


def test_every_numeric_column_declares_precision_and_scale():
    offenders = [
        f'{table.name}.{column.name}'
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, Numeric)
        and not isinstance(column.type, Float)
        and (column.type.precision is None or column.type.scale is None)
    ]
    assert offenders == []


def test_every_table_has_a_primary_key():
    for table in METADATA.tables.values():
        assert list(table.primary_key.columns), table.name


# ---------------------------------------------------------------------------
# Rule 1: label-only columns are unreachable from the feature path
# ---------------------------------------------------------------------------
def test_label_only_columns_exist_on_the_label():
    for name in LABEL_ONLY_COLUMNS:
        assert name in Label.__table__.columns


def test_label_only_columns_exist_nowhere_else():
    """Not on feature_row, not on candidate, not on any feed."""
    for name in LABEL_ONLY_COLUMNS:
        for table in METADATA.tables.values():
            if table.name == 'label':
                continue
            assert name not in table.columns, f'{name} must live only on label, found on {table.name}'


def test_the_label_is_not_a_feed_and_cannot_be_gated():
    assert not issubclass(Label, FeedRow)
    assert Label not in FEED_MODELS
    assert Candidate not in FEED_MODELS
    assert FeatureRow not in FEED_MODELS


def test_the_decision_time_residual_has_a_different_name_from_the_hold_residual():
    """So nobody reaches for the label column looking for 'the residual'."""
    assert 'alt_residual_z_signal_bar' in FeatureRow.__table__.columns
    assert 'cmi_alt_residual_return' in Label.__table__.columns
    assert 'cmi_alt_residual_return' in LABEL_ONLY_COLUMNS


def test_mfe_diagnostics_are_on_the_label_only():
    for name in MFE_DIAGNOSTIC_COLUMNS:
        assert name in Label.__table__.columns
        assert name not in FeatureRow.__table__.columns


# ---------------------------------------------------------------------------
# Rule 2: columns for feeds that do not exist yet
# ---------------------------------------------------------------------------
ABSENT_FEED_COLUMNS = (
    'cme_basis_z',
    'etf_net_flow_z',
    'ibit_dealer_gamma_sign',
    'cme_dealer_gamma_sign',
)


@pytest.mark.parametrize('name', ABSENT_FEED_COLUMNS)
def test_columns_for_feeds_that_do_not_exist_yet_are_present_and_nullable(name):
    column = FeatureRow.__table__.columns[name]
    assert column.nullable, f'{name} must accept NULL: a missing print is NULL, never zero'
    assert column.server_default is None, f'{name} must not be zero-filled by a default'


def test_the_ibit_and_cme_venues_are_addressable_even_without_a_feed():
    assert E.OPTIONS_VENUE_IBIT in E.OPTIONS_VENUES
    assert E.OPTIONS_VENUE_CME in E.OPTIONS_VENUES


def test_expiry_size_is_undecided_rather_than_false():
    column = METADATA.tables['event_calendar'].columns['is_large']
    assert column.nullable
    assert column.server_default is None


# ---------------------------------------------------------------------------
# Shadow metrics stay in the bag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('key', SHADOW_METRIC_KEYS)
def test_no_shadow_metric_has_a_typed_feature_column(key):
    assert key not in FeatureRow.__table__.columns


def test_the_shadow_bag_is_not_nullable_so_it_is_always_an_object():
    column = FeatureRow.__table__.columns['shadow_metrics']
    assert not column.nullable
    assert column.server_default is not None


# ---------------------------------------------------------------------------
# Feeds carry both times
# ---------------------------------------------------------------------------
ECONOMIC_TIME_COLUMN = {
    'candle': 'close_time',
    'open_interest': 'poll_time',
    'funding': 'funding_time',
    'force_order': 'traded_at',
    'event_calendar': 'event_time',
    'options_snapshot': 'snapshot_time',
    'options_metric': 'snapshot_time',
}


@pytest.mark.parametrize('model', FEED_MODELS, ids=lambda m: m.__tablename__)
def test_every_feed_carries_its_economic_time_and_available_at(model):
    table = model.__table__
    assert 'available_at' in table.columns
    assert not table.columns['available_at'].nullable
    economic = ECONOMIC_TIME_COLUMN[table.name]
    assert economic in table.columns
    assert economic != 'available_at'


@pytest.mark.parametrize('model', FEED_MODELS, ids=lambda m: m.__tablename__)
def test_every_feed_has_an_index_covering_available_at(model):
    """The gate's filter must be index-supported on every feed table.

    Most tables put ``available_at`` last behind equality columns (coin, venue,
    timeframe); ``event_calendar`` leads with it because calendar reads are not
    coin-scoped.
    """
    indexed = {
        column.name for index in model.__table__.indexes for column in index.columns
    }
    assert 'available_at' in indexed, f'{model.__tablename__} has no index supporting the as_of gate'


def test_feed_health_is_not_a_gated_feed():
    """It describes the feeds; it does not carry a print."""
    assert 'available_at' not in METADATA.tables['feed_health'].columns


# ---------------------------------------------------------------------------
# Natural uniqueness
# ---------------------------------------------------------------------------
EXPECTED_UNIQUE_COLUMNS = {
    'candle': {('coin', 'market', 'timeframe', 'open_time')},
    'open_interest': {('coin', 'timeframe', 'poll_time')},
    'funding': {('coin', 'funding_time', 'kind')},
    'force_order': {('dedup_key',)},
    'event_calendar': {('kind', 'coin', 'event_time')},
    'feed_health': {('feed', 'coin')},
    'options_snapshot': {('venue', 'coin', 'snapshot_time')},
    'options_metric': {('venue', 'coin', 'snapshot_time', 'horizon')},
    'feature_row': {
        ('coin', 'horizon', 'signal_timeframe', 'bar_close', 'source'),
        ('id', 'coin', 'horizon', 'bar_close', 'source'),
    },
    'candidate': {
        ('coin', 'horizon', 'bar_close', 'source'),
        ('id', 'sample_group_id', 'horizon', 'source'),
    },
    'promotion_log': {('layer', 'decided_at')},
}


@pytest.mark.parametrize(('table_name', 'expected'), sorted(EXPECTED_UNIQUE_COLUMNS.items()))
def test_natural_uniqueness_is_declared(table_name, expected):
    table = METADATA.tables[table_name]
    found = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == 'UniqueConstraint'
    }
    assert found == expected


def test_a_venue_is_part_of_every_options_key():
    for name in ('options_snapshot', 'options_metric'):
        table = Base.metadata.tables[name]
        unique = next(
            constraint
            for constraint in table.constraints
            if constraint.__class__.__name__ == 'UniqueConstraint'
        )
        assert 'venue' in [column.name for column in unique.columns], (
            f'{name}: Deribit and IBIT are scored separately and never blended'
        )


# ---------------------------------------------------------------------------
# Vocabularies come from the frozen constants, not from here
# ---------------------------------------------------------------------------
def test_vocabularies_are_re_exported_rather_than_restated():
    from app.core import constants as C

    assert E.HORIZONS is C.HORIZONS
    assert E.TREND_FAMILIES is C.FAMILIES
    assert E.OPTIONS_VERDICTS is C.OPTIONS_VERDICT_PRECEDENCE
    assert E.OPTIONS_EXPRESSIONS is C.OPTIONS_EXPRESSIONS
    assert E.OPTIONS_VOL_TAGS is C.OPTIONS_VOL_TAGS
    assert E.STAND_DOWN_EVENT_KINDS == tuple(C.EVENT_WINDOW_MINUTES)
    assert E.POOL_LEVELS == tuple(level for level, _ in C.POOL_WALK_UP_LEVELS)


def test_no_native_postgres_enum_is_used():
    """Closed vocabularies are text plus a CHECK; see app/models/enums.py."""
    offenders = [
        f'{table.name}.{column.name}'
        for table in METADATA.tables.values()
        for column in table.columns
        if column.type.__class__.__name__ in {'ENUM', 'Enum'}
    ]
    assert offenders == []


def test_every_closed_vocabulary_column_has_a_check():
    checked = {
        table.name: ' '.join(
            str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.__class__.__name__ == 'CheckConstraint'
        )
        for table in METADATA.tables.values()
    }
    required = {
        'candidate': ('horizon', 'direction', 'source', 'status', 'trigger', 'sample_group_reason'),
        'feature_row': ('horizon', 'source', 'signal_timeframe'),
        'label': ('first_barrier',),
        'candle': ('timeframe', 'market'),
        'funding': ('kind',),
        'options_metric': ('venue', 'horizon'),
        'options_snapshot': ('venue',),
        'event_calendar': ('kind',),
    }
    for table_name, columns in required.items():
        for column in columns:
            assert re.search(rf'\b{column} IN \(', checked[table_name]), f'{table_name}.{column}'


def test_signs_are_small_integers_constrained_to_three_values():
    sign_columns = [
        (table, column)
        for table in METADATA.tables.values()
        for column in table.columns
        if column.name.endswith('dealer_gamma_sign')
    ]
    assert sign_columns
    for table, column in sign_columns:
        assert isinstance(column.type, SmallInteger), f'{table.name}.{column.name}'
        checks = ' '.join(
            str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.__class__.__name__ == 'CheckConstraint'
        )
        assert f'{column.name} IN (-1, 0, 1)' in checks


# ---------------------------------------------------------------------------
# The feature path discipline, enforced on the source tree
# ---------------------------------------------------------------------------
APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / 'app'
FEED_CLASS_NAMES = tuple(model.__name__ for model in FEED_MODELS)
BARE_READ = re.compile(
    r'(?<![\w.])(?:select|query)\(\s*(' + '|'.join(FEED_CLASS_NAMES) + r')\b'
)


def test_no_module_outside_app_models_reads_a_feed_without_the_gate():
    """Every feed read goes through as_of(), including the resolver's.

    The resolver and ingest are not exceptions: they call ``as_of(utc_now())`` or
    ``as_of(replay_clock)``. There is one read path for feeds, not a disciplined
    one and a convenient one. If this test fails on your new module, you wanted
    ``as_of(instant).select(...)`` or ``as_of(instant).gate(statement)``.
    """
    offenders = []
    for path in APP_ROOT.rglob('*.py'):
        if 'models' in path.relative_to(APP_ROOT).parts:
            continue
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if BARE_READ.search(line):
                offenders.append(f'{path.relative_to(APP_ROOT)}:{number}: {line.strip()}')
    assert offenders == [], '\n'.join(offenders)
