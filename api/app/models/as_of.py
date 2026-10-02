"""The available-at read gate.

The spec's rule: "Join on available-at, the time the print could have been
known, not the economic time it describes." A feature computed from a print that
had not been published when the signal bar closed is a leak, and a leak that
only shows up as unexplained optimism months later.

So raw feed rows are never read with a bare ``select()``. Every read passes
through this module::


    from app.models.as_of import as_of
    from app.models.feeds import Candle

    reader = as_of(bar_close)
    stmt = (
        reader.select(Candle)
        .where(Candle.coin == 'BTCUSDT', Candle.market == 'perp', Candle.timeframe == '15m')
        .order_by(Candle.open_time.desc())
        .limit(constants.CVD_BARS)
    )
    bars = session.execute(stmt).scalars().all()

``reader.select(...)`` has already added ``candle.available_at <= bar_close``.
There is no argument you can pass to switch that off, and ``select`` refuses any
entity that is not a feed table, so the only thing it can ever return is a gated
selectable.

Two entry points, because there are two shapes of read:

``AsOf.select(*entities)``
    The normal case. Builds the ``Select`` for you and gates every feed entity
    in it. Rejects non-feed entities outright, so ``as_of(t).select(Label)``
    raises instead of quietly handing back an ungated label query.
``AsOf.gate(statement)``
    For a statement you had to build yourself - a join against ``candidate``,
    an aggregate, a window function. It finds every feed table in the statement,
    including aliases, and adds the filter to each. It raises if the statement
    contains no feed table at all (you reached for the wrong tool) and if a feed
    table is hidden inside a subquery or CTE (gate the inner ``select`` instead,
    where the filter can actually be applied).

``AsOf.latest`` is a convenience for the single most common decision-time read:
"the newest row of this feed that I was allowed to see". The last **settled**
funding print is exactly that.

The instant
-----------
For a decision-time feature read the instant is the signal bar's close, and the
comparison is inclusive: a kline whose ``available_at`` equals ``bar_close`` is
the signal bar itself, which is by definition known - the entry is the *next*
bar's open.


For the barrier resolver and for ingest the same gate applies with a different
instant: ``as_of(utc_now())`` live, or ``as_of(replay_clock)`` in replay. That
is deliberate. There is one read path for feeds, not a disciplined one and a
convenient one.

What is not reachable through here
----------------------------------
``feature_row``, ``candidate`` and ``label`` are not feed tables and
:meth:`AsOf.select` refuses them. That is what makes the spec's "Label-only
columns are written at resolution and are forbidden as decision-time features"
structural rather than aspirational: the label's cross-market impulse columns
cannot be reached from the only sanctioned decision-time read path, with any
argument.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, Table
from sqlalchemy import select as sa_select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.selectable import CTE, Alias, FromClause, Join, Subquery

from app.core.time import ensure_utc
from app.models.base import FeedRow
from app.models.feeds import Candle, EventCalendar, ForceOrder, Funding, OpenInterest
from app.models.options import OptionsMetric, OptionsSnapshot

#: Every table that carries ``available_at`` and is therefore gated. A table is
#: enrolled by subclassing :class:`~app.models.base.FeedRow` AND being listed
#: here; ``tests/test_as_of.py`` fails if a mapped ``FeedRow`` subclass is
#: missing from this tuple, so a feed added by a later milestone cannot silently
#: escape the gate.
FEED_MODELS: tuple[type[FeedRow], ...] = (
    Candle,
    OpenInterest,
    Funding,
    ForceOrder,
    EventCalendar,
    OptionsSnapshot,
    OptionsMetric,
)

_FEED_TABLE_NAMES = frozenset(model.__tablename__ for model in FEED_MODELS)


class AsOfError(ValueError):
    """A read was framed in a way the available-at gate cannot guarantee."""


def _owning_entity(entity: Any) -> type[Any] | None:
    """Model class behind a select() argument, or None if there is not one."""
    if isinstance(entity, InstrumentedAttribute):
        return entity.parent.class_
    if isinstance(entity, type):
        return entity
    return None


def _is_feed_table(obj: Any) -> bool:
    return isinstance(obj, Table) and obj.name in _FEED_TABLE_NAMES


def _feed_from_clauses(from_clause: FromClause) -> Iterator[FromClause]:
    """Yield each FROM element that is a feed table, following joins and aliases."""
    if isinstance(from_clause, Join):
        yield from _feed_from_clauses(from_clause.left)
        yield from _feed_from_clauses(from_clause.right)
    elif isinstance(from_clause, (Subquery, CTE)):
        inner = from_clause.element
        if isinstance(inner, Select) and _contains_feed_table(inner):
            raise AsOfError(
                'a feed table is hidden inside a subquery or CTE, where the available-at filter '
                'cannot be applied to the outer query. Gate the inner select instead: '
                'as_of(bar_close).select(...).subquery()'
            )
    elif isinstance(from_clause, Alias):
        element = from_clause.element
        if _is_feed_table(element):
            yield from_clause
        elif _contains_feed_table_fromclause(element):
            raise AsOfError(
                f'cannot gate the aliased construct {from_clause!r}: alias a feed table directly, '
                'or gate the statement that builds it'
            )
    elif _is_feed_table(from_clause):
        yield from_clause


def _contains_feed_table_fromclause(from_clause: FromClause) -> bool:
    if _is_feed_table(from_clause):
        return True
    if isinstance(from_clause, Join):
        return _contains_feed_table_fromclause(from_clause.left) or _contains_feed_table_fromclause(from_clause.right)
    if isinstance(from_clause, (Alias, Subquery, CTE)):
        element = from_clause.element
        if isinstance(element, Select):
            return _contains_feed_table(element)
        if isinstance(element, FromClause):
            return _contains_feed_table_fromclause(element)
    return False


def _contains_feed_table(statement: Select) -> bool:
    return any(_contains_feed_table_fromclause(from_) for from_ in statement.get_final_froms())


@dataclass(frozen=True)
class AsOf:
    """A read gate pinned to one instant. Build it with :func:`as_of`."""

    bar_close: datetime

    # -- building a gated read ---------------------------------------------
    def select(self, *entities: Any) -> Select[Any]:
        """``select(*entities)`` with the available-at filter already applied.

        Every entity must be a feed model or a column of one. Anything else -
        a feature row, a candidate, a label, a raw ``Table`` - raises
        :class:`AsOfError`, because this gate can only make a promise about
        tables that carry ``available_at``.
        """
        if not entities:
            raise AsOfError('select() needs at least one feed entity')
        models: list[type[FeedRow]] = []
        for entity in entities:
            owner = _owning_entity(entity)
            if owner is None or owner not in FEED_MODELS:
                raise AsOfError(
                    f'{entity!r} is not a feed table. as_of() gates raw feeds only: {_feed_names()}. '
                    'feature_row, candidate and label are not read through the available-at gate; '
                    'label columns in particular are written at resolution and are forbidden as '
                    'decision-time features.'
                )
            if owner not in models:
                models.append(owner)
        statement = sa_select(*entities)
        for model in models:
            statement = statement.where(model.available_at <= self.bar_close)
        return statement

    def gate(self, statement: Select[Any]) -> Select[Any]:
        """Add the available-at filter to every feed table in ``statement``.

        Use this for a statement you had to build yourself. Raises if there is no
        feed table to gate, or if one is hidden where the filter cannot reach it.
        """
        if not isinstance(statement, Select):
            raise AsOfError(f'gate() takes a Select, got {type(statement).__name__}')
        gated = 0
        for from_ in statement.get_final_froms():
            for feed in _feed_from_clauses(from_):
                statement = statement.where(feed.c.available_at <= self.bar_close)
                gated += 1
        if gated == 0:
            raise AsOfError(
                'this statement reads no feed table, so there is nothing for the available-at gate '
                'to do. Build it with a plain select(); only raw feeds are gated.'
            )
        return statement

    # -- running one ------------------------------------------------------
    def latest(
        self,
        session: Session,
        model: type[FeedRow],
        *,
        order_by: ColumnElement[Any] | None = None,
        **equals: Any,
    ) -> Any | None:
        """Newest visible row of ``model``, or None.

        ``equals`` becomes equality filters, so the canonical "last settled
        funding print I was allowed to see" is::

            as_of(bar_close).latest(session, Funding, coin='BTCUSDT', kind='settled',
                                    order_by=Funding.funding_time)

        ``order_by`` defaults to ``available_at``. Pass the economic column when
        "newest" means newest print rather than newest arrival.
        """
        statement = self.select(model)
        for column, value in equals.items():
            statement = statement.where(getattr(model, column) == value)
        ordering = model.available_at if order_by is None else order_by
        statement = statement.order_by(ordering.desc(), model.id.desc()).limit(1)
        return session.execute(statement).scalars().first()

    def rows(self, session: Session, statement: Select[Any]) -> Sequence[Any]:
        """Run a statement built by :meth:`select` or :meth:`gate`. Convenience only."""
        return session.execute(statement).scalars().all()


def _feed_names() -> str:
    return ', '.join(sorted(_FEED_TABLE_NAMES))


def as_of(bar_close: datetime) -> AsOf:
    """Read gate for everything knowable at ``bar_close``.

    ``bar_close`` is the signal bar's close for a decision-time feature read, or
    ``app.core.time.utc_now()`` (live) / the replay clock (replay) for the
    barrier resolver and ingest. It must be timezone-aware; a naive datetime
    raises, because guessing the zone of the instant that defines every label in
    the system is not an option.
    """
    return AsOf(ensure_utc(bar_close))


__all__ = ['FEED_MODELS', 'AsOf', 'AsOfError', 'as_of']
