"""SQLAlchemy models for the Signals loop.

Importing this package imports every model module, so ``Base.metadata`` is
complete. ``alembic/env.py`` points ``target_metadata`` here and nowhere else;
a table that is not reachable from this import is not in a migration.

One module per concern:

``base``
    Declarative base, the exact-numeric column aliases, the mixins.
``enums``
    Closed vocabularies and the ``CHECK`` helpers that enforce them.
``feeds``
    Raw feeds, each carrying its economic time and ``available_at``.
``options``
    Option book snapshots and the figures derived from them, keyed by venue.
``features``
    The decision-time feature store.
``decisions``
    Candidates, and the ``sample_group_id`` independence design.
``labels``
    Resolved outcomes, including the label-only columns.
``observability``
    Job runs and the promotion gate's decision log.
``as_of``
    The available-at read gate. Every feed read goes through it.

There is no order table, no position table and no execution table here, in any
phase. Phase 3 may read positions; nothing may ever send an order.
"""

from __future__ import annotations

from app.models import enums
from app.models.base import Base, FeedRow
from app.models.as_of import FEED_MODELS, AsOf, AsOfError, as_of
from app.models.decisions import Candidate
from app.models.features import SHADOW_METRIC_KEYS, FeatureRow
from app.models.feeds import Candle, EventCalendar, FeedHealth, ForceOrder, Funding, OpenInterest
from app.models.labels import LABEL_ONLY_COLUMNS, MFE_DIAGNOSTIC_COLUMNS, Label
from app.models.observability import JobRun, PromotionLog
from app.models.options import OptionsMetric, OptionsSnapshot

metadata = Base.metadata

__all__ = [
    'FEED_MODELS',
    'LABEL_ONLY_COLUMNS',
    'MFE_DIAGNOSTIC_COLUMNS',
    'SHADOW_METRIC_KEYS',
    'AsOf',
    'AsOfError',
    'Base',
    'Candidate',
    'Candle',
    'EventCalendar',
    'FeatureRow',
    'FeedHealth',
    'FeedRow',
    'ForceOrder',
    'Funding',
    'JobRun',
    'Label',
    'OpenInterest',
    'OptionsMetric',
    'OptionsSnapshot',
    'PromotionLog',
    'as_of',
    'enums',
    'metadata',
]
