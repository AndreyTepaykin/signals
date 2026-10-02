"""Declarative base, shared column types and table conventions.

Rules that hold for every table in this package, stated once here:

* Every instant is ``timestamptz`` and UTC. There is no naive timestamp column
  anywhere, and no ``date`` column except where a session date is meant.
* Every price, size, rate, R value and ATR distance is exact ``numeric``.
  ``float`` / ``double precision`` never appears: a label that survives months
  of accumulation must not drift.
* Column names carry their unit. ``*_atr`` is a distance in ATR units,
  ``*_z`` is a 90-day z-score, ``*_pct`` is a 0-1 quantile, ``*_bps`` is basis
  points, ``*_r`` is a multiple of 1R, ``*_price`` is a price in quote terms.
* Closed vocabularies are ``text`` plus a ``CHECK`` constraint (see
  ``app.models.enums``), never a Postgres native ``ENUM``.
* Raw feed tables carry BOTH the economic instant they describe and
  ``available_at``, the instant the print could first have been known. They are
  read only through :func:`app.models.as_of.as_of`.

There is no order table, no position table and no execution table in this
package, and there will not be one in any phase.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Identity, Integer, MetaData, Numeric, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Constraint and index naming
# ---------------------------------------------------------------------------
# Deterministic names so that an Alembic downgrade can drop exactly what the
# upgrade created. Postgres truncates identifiers at 63 bytes, so any
# constraint whose generated name would be longer is named explicitly at the
# table instead of relying on this convention.
NAMING_CONVENTION = {
    'pk': 'pk_%(table_name)s',
    'uq': 'uq_%(table_name)s_%(column_0_N_name)s',
    'ix': 'ix_%(table_name)s_%(column_0_N_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_N_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
}

# ---------------------------------------------------------------------------
# Exact numeric column types, one alias per unit
# ---------------------------------------------------------------------------
# The scale is chosen per unit and is part of the contract: widening one later
# is a migration, not an edit. Precision leaves room for the largest value the
# unit can plausibly take (a 1e13 quote volume, a 1e6 price) without ever
# falling back on binary floating point.
PRICE = Numeric(24, 10)  # prices, strikes, barriers (quote terms)
SIZE = Numeric(32, 10)  # volumes, open interest, notional
RATE = Numeric(18, 12)  # funding rates and plain returns (0.0001 = 1 bp)
R_VALUE = Numeric(14, 6)  # multiples of 1R
ATR_UNITS = Numeric(14, 6)  # distances expressed in ATR
Z_SCORE = Numeric(14, 6)  # 90-day z-scores
QUANTILE = Numeric(7, 6)  # percentiles as a 0-1 quantile
BPS = Numeric(14, 6)  # basis points
RATIO = Numeric(12, 8)  # shares, fractions, efficiency readings
MULTIPLIER = Numeric(8, 4)  # size multipliers
HOURS = Numeric(12, 4)  # durations in hours
INFLUENCE = Numeric(6, 5)  # options_influence, -1 to +1

Price = Annotated[Decimal, 'price']
Size = Annotated[Decimal, 'size']
Rate = Annotated[Decimal, 'rate']
RValue = Annotated[Decimal, 'r_value']
AtrUnits = Annotated[Decimal, 'atr_units']
ZScore = Annotated[Decimal, 'z_score']
Quantile = Annotated[Decimal, 'quantile']
Bps = Annotated[Decimal, 'bps']
Ratio = Annotated[Decimal, 'ratio']
Multiplier = Annotated[Decimal, 'multiplier']
Hours = Annotated[Decimal, 'hours']
Influence = Annotated[Decimal, 'influence']

#: A signed direction flag stored as ``smallint`` constrained to -1 / 0 / +1
#: (dealer gamma sign). Not a boolean: zero means "flat / undetermined".
Sign = Annotated[int, 'sign']


class Base(DeclarativeBase):
    """Shared declarative base. ``Base.metadata`` is Alembic's target."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        datetime: DateTime(timezone=True),
        date: Date(),
        str: Text(),
        int: Integer(),
        bool: Boolean(),
        uuid.UUID: PG_UUID(as_uuid=True),
        dict[str, Any]: JSONB(),
        Decimal: PRICE,  # never relied on implicitly; every column names its alias
        Price: PRICE,
        Size: SIZE,
        Rate: RATE,
        RValue: R_VALUE,
        AtrUnits: ATR_UNITS,
        ZScore: Z_SCORE,
        Quantile: QUANTILE,
        Bps: BPS,
        Ratio: RATIO,
        Multiplier: MULTIPLIER,
        Hours: HOURS,
        Influence: INFLUENCE,
        Sign: SmallInteger(),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        pk = getattr(self, 'id', None)
        return f'<{type(self).__name__} id={pk!r}>'


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------
class SurrogateKey:
    """``bigint`` identity primary key.

    Natural keys are enforced by UNIQUE constraints instead of being used as
    the primary key, so that a table can be referenced by one column and a
    natural key can be widened without rewriting every reference.
    """

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True, sort_order=-100)


class CreatedAt:
    """``created_at`` stamped by the database, for forensics only.

    It is never a feature and never an available-at substitute.
    """

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class FeedRow(SurrogateKey, CreatedAt):
    """A raw feed row: it carries ``available_at`` and is read only via ``as_of``.

    ``available_at`` is the instant the print could first have been known - a
    bar's close, a poll's completion, a funding settlement, a snapshot time -
    and never the economic instant the row describes. Those are separate
    columns on every subclass precisely because they differ.

    Subclassing this is what enrols a table in
    ``app.models.as_of.FEED_MODELS``; see that module's registry test.
    """

    available_at: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='Instant this print could first have been known. Decision-time reads filter on it.',
    )
    # No single-column index here on purpose: every real read is scoped by coin
    # (or by venue and coin), so each table declares a composite index ending in
    # available_at instead of carrying a redundant one.


__all__ = [
    'ATR_UNITS',
    'BPS',
    'HOURS',
    'INFLUENCE',
    'MULTIPLIER',
    'NAMING_CONVENTION',
    'PRICE',
    'QUANTILE',
    'RATE',
    'RATIO',
    'R_VALUE',
    'SIZE',
    'Z_SCORE',
    'AtrUnits',
    'Base',
    'Bps',
    'CreatedAt',
    'FeedRow',
    'Hours',
    'Influence',
    'Multiplier',
    'Price',
    'Quantile',
    'RValue',
    'Rate',
    'Ratio',
    'Sign',
    'Size',
    'SurrogateKey',
    'ZScore',
]
