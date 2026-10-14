"""The promotion job: run the shadow promotion gate over labeled candidates and print one verdict per cell.

``python -m app.worker.promotion_job [--layer NAME | --all] [--source live|replay] [--as-of ISO] [--json] [--record]``

Read-only by default: the session is READ ONLY at the database level (`stats_job.readonly_session`) and always rolled
back. ``--record`` is the one write: it inserts one ``promotion_decision`` row per cell and nothing else. Nothing here
touches a candidate, a label or a live decision; promoting a layer is a human ``RULE_VERSION`` bump.

What it reads
-------------
* Observations through `stats_job.load_observations` (candidate AND label on the current ``rule_hash``).
* For every locked-book row a layer blocked, the `Counterfactual` the pure layers need: what the rest of the frozen
  ladder would have decided with that gate switched off. It is recomputed with the same pure functions the rules and the
  options job ran, from the same stored inputs:

  - the positioning vote, `direction.tally` on the candidate's feature row (a CVD cancel fires before the vote check,
    so the vote was never recorded);
  - the options check on a PENDING copy of the row, `options_job.load_check_inputs` + `check_options` (the book is read
    through ``as_of(decision_time)``, so the answer is the one the job gave at the time). The recomputed verdict, stop
    and cap must equal the stored ones, or the row gets no counterfactual;
  - the abstain rule, `abstain.check_abstain`, on the stored pooled estimate and the feature row's funding carry.

  A blocked row without a counterfactual is out of the pair (see `app.core.promotion.layers`).

``as_of``: the clock for live; for replay, the latest ``resolved_at`` among the replay observations (a backtest's shadow
window ends where its data ends). ``--as-of`` overrides both.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.hashing import code_version as current_code_version
from app.core.hashing import rule_hash as current_rule_hash
from app.core.options.check import check_options
from app.core.promotion import LAYERS, Counterfactual, GateCell, Layer, get_layer, run_gate
from app.core.rules import abstain as AB
from app.core.rules import direction as D
from app.core.rules.inputs import RuleInputs
from app.core.stats.observations import Observation
from app.core.time import ensure_utc, utc_now
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.promotion import PromotionDecision
from app.worker import options_job as OJ
from app.worker.feature_job import REFERENCE_COIN
from app.worker.stats_job import load_observations, readonly_session

PRICE_QUANTUM = Decimal('1e-10')  # candidate price columns are Numeric(24, 10)

CF_COMPUTED = 'computed'
CF_NO_CANDIDATE = 'no_candidate'
CF_NO_FEATURE_ROW = 'no_feature_row'
CF_NO_CHECK_INPUTS = 'no_check_inputs'
CF_MISMATCH = 'options_recompute_mismatch'


def _same_price(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(a).quantize(PRICE_QUANTUM) == Decimal(b).quantize(PRICE_QUANTUM)


def counterfactual_for(session: Session, candidate: Candidate) -> tuple[Counterfactual | None, str]:
    """What the rest of the ladder decides for ``candidate`` with its own blocking gate off. Reads only."""
    row = session.get(FeatureRow, candidate.feature_row_id)
    if row is None:
        return None, CF_NO_FEATURE_ROW
    vote = D.tally(candidate.direction, RuleInputs.from_feature_row(row, reference_coin=REFERENCE_COIN))
    inputs, _why = OJ.load_check_inputs(session, candidate)
    if inputs is None:
        return None, CF_NO_CHECK_INPUTS
    decision = check_options(replace(inputs, status=E.STATUS_WATCH, status_reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK))
    if (
        decision.verdict != candidate.options_verdict
        or not _same_price(decision.stop_price, candidate.stop_price)
        or not _same_price(decision.target_cap_price, candidate.target_cap_price)
    ):
        return None, CF_MISMATCH
    status = decision.status
    abstain = AB.check_abstain(
        status=status,
        pool_n=candidate.pool_n,
        expected_r_ci_low=None if candidate.expected_r_ci_low is None else float(candidate.expected_r_ci_low),
        funding_carry_bps=row.funding_carry_bps_horizon,
        reference_price=inputs.reference_price,
        stop_price=decision.stop_price,
    )
    if abstain.abstain:
        status = E.STATUS_ABSTAINED
    return Counterfactual(vote_confirmed=vote.confirmed, status_if_pending=status), CF_COMPUTED


def load_counterfactuals(
    session: Session, observations: Iterable[Observation], layers: Iterable[Layer]
) -> tuple[dict[int, Counterfactual], dict[str, int]]:
    """Counterfactuals for the locked-book rows any of ``layers`` blocked, plus how each attempt ended."""
    layer_list = list(layers)
    wanted = sorted(
        {o.candidate_id for o in observations if o.locked_book is True and any(layer.blocks(o) for layer in layer_list)}
    )
    facts: dict[int, Counterfactual] = {}
    outcomes: Counter[str] = Counter()
    for candidate_id in wanted:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            outcomes[CF_NO_CANDIDATE] += 1
            continue
        fact, outcome = counterfactual_for(session, candidate)
        outcomes[outcome] += 1
        if fact is not None:
            facts[candidate_id] = fact
            outcomes[f'status_if_pending:{fact.status_if_pending}'] += 1
            outcomes[f'vote_confirmed:{fact.vote_confirmed}'] += 1
    return facts, dict(sorted(outcomes.items()))


def default_as_of(source: str, observations: Sequence[Observation], now: datetime) -> datetime:
    if source == E.SOURCE_REPLAY and observations:
        return max(ensure_utc(o.resolved_at) for o in observations)
    return now


def run_promotion(
    session: Session,
    *,
    layers: Sequence[Layer],
    sources: Sequence[str],
    as_of: datetime | None = None,
    now: datetime | None = None,
    rule_hash: str | None = None,
) -> tuple[list[GateCell], dict[str, Any]]:
    """Every cell for ``layers`` x ``sources``. Reads only. Returns the cells and what was loaded."""
    wanted = rule_hash or current_rule_hash()
    clock = ensure_utc(now) if now is not None else utc_now()
    cells: list[GateCell] = []
    context: dict[str, Any] = {'rule_hash': wanted, 'sources': {}}
    for source in sources:
        loaded = load_observations(session, rule_hash=wanted, source=source)
        observations = [
            replace(o, decision_time=ensure_utc(o.decision_time), bar_close=ensure_utc(o.bar_close), resolved_at=ensure_utc(o.resolved_at))
            for o in loaded.observations
        ]
        instant = ensure_utc(as_of) if as_of is not None else default_as_of(source, observations, clock)
        facts, outcomes = load_counterfactuals(session, observations, layers)
        context['sources'][source] = {
            'as_of': instant.isoformat(),
            'observations': len(observations),
            'locked_book': sum(1 for o in observations if o.locked_book is True),
            'locked_book_unknown': sum(1 for o in observations if o.locked_book is None),
            'counterfactuals': outcomes,
        }
        for layer in layers:
            cells.extend(run_gate(layer, observations, as_of=instant, counterfactuals=facts))
    return cells, context


def _decimal(value: float | None, places: str) -> Decimal | None:
    return None if value is None else Decimal(repr(value)).quantize(Decimal(places))


def decision_row(cell: GateCell, *, rule_hash: str, code_version: str) -> PromotionDecision:
    return PromotionDecision(
        layer=cell.layer,
        source=cell.cohort.source,
        options_path=cell.cohort.options_path,
        lookback=cell.cohort.lookback,
        horizon=cell.horizon,
        verdict=cell.verdict,
        binding=cell.binding,
        n_pairs=cell.n_pairs,
        mean_diff_r=_decimal(cell.mean_diff_r, '0.000001'),
        ci_low=_decimal(cell.ci_low, '0.000001'),
        ci_high=_decimal(cell.ci_high, '0.000001'),
        shadow_start=cell.shadow_start,
        as_of=cell.as_of,
        window_days=_decimal(cell.window_days, '0.0001'),
        params={**dict(cell.params), 'counts': dict(cell.counts)},
        reasons=list(cell.reasons),
        rule_hash=rule_hash,
        code_version=code_version,
    )


def record_cells(session: Session, cells: Sequence[GateCell], *, rule_hash: str, code_version: str) -> int:
    """Append one ``promotion_decision`` row per cell. Does not commit."""
    rows = [decision_row(cell, rule_hash=rule_hash, code_version=code_version) for cell in cells]
    session.add_all(rows)
    session.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def cell_to_jsonable(cell: GateCell) -> dict[str, Any]:
    return {
        'layer': cell.layer,
        'cohort': cell.cohort.label,
        'horizon': cell.horizon,
        'verdict': cell.verdict,
        'binding': cell.binding,
        'n_pairs': cell.n_pairs,
        'n_blocked_pairs': cell.n_blocked_pairs,
        'mean_diff_r': cell.mean_diff_r,
        'ci_low': cell.ci_low,
        'ci_high': cell.ci_high,
        'shadow_start': None if cell.shadow_start is None else cell.shadow_start.isoformat(),
        'as_of': cell.as_of.isoformat(),
        'window_days': cell.window_days,
        'reasons': list(cell.reasons),
        'counts': dict(cell.counts),
        'params': dict(cell.params),
    }


def _num(value: float | None) -> str:
    return '-' if value is None else f'{value:+.3f}'


def format_text(cells: Sequence[GateCell], context: dict[str, Any]) -> str:
    lines = [f"Promotion gate (report only; binds on live). rule_hash={context['rule_hash']}"]
    for source, info in context['sources'].items():
        lines.append(
            f"  {source}: as_of={info['as_of']} observations={info['observations']} locked_book={info['locked_book']} "
            f"locked_book_unknown={info['locked_book_unknown']} counterfactuals={info['counterfactuals']}"
        )
    header = f"{'layer':<22} {'cohort':<36} {'horizon':<8} {'verdict':<21} {'pairs':>5} {'blk':>4} {'mean':>7} {'ci90':>17} {'days':>6}"
    lines += ['', header, '-' * len(header)]
    for cell in cells:
        ci = '-' if cell.ci_low is None else f'[{_num(cell.ci_low)},{_num(cell.ci_high)}]'
        lines.append(
            f'{cell.layer:<22} {cell.cohort.label:<36} {cell.horizon:<8} {cell.verdict:<21} {cell.n_pairs:>5} '
            f'{cell.n_blocked_pairs:>4} {_num(cell.mean_diff_r):>7} {ci:>17} {cell.window_days:>6.1f}'
        )
        c = cell.counts
        lines.append(
            f"    rows={c['rows']} locked={c['locked_book']} independent={c['independent']} no_entry={c['no_entry']} "
            f"blocked={c['blocked_by_layer']} blocked_paired={c['blocked_paired']} not_in_pair={c['not_in_pair']} "
            f"embargoed={c['embargoed']}"
        )
        lines.extend(f'    - {reason}' for reason in cell.reasons)
    if not cells:
        lines.append('(no locked-book observations)')
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.db_sync import get_sync_engine, get_sync_session_factory

    parser = argparse.ArgumentParser(description='Shadow promotion gate (report only; --record appends to promotion_decision).')
    which = parser.add_mutually_exclusive_group()
    which.add_argument('--layer', choices=sorted(LAYERS))
    which.add_argument('--all', action='store_true', help='every registered layer (the default)')
    parser.add_argument('--source', choices=E.SOURCES, help='default: both, each its own cells')
    parser.add_argument('--as-of', help='ISO-8601 instant (default: now for live, last resolution for replay)')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--record', action='store_true', help='append one promotion_decision row per cell (the only write)')
    args = parser.parse_args(argv)

    layers = [get_layer(args.layer)] if args.layer else list(LAYERS.values())
    sources = [args.source] if args.source else list(E.SOURCES)
    as_of = ensure_utc(datetime.fromisoformat(args.as_of.replace('Z', '+00:00'))) if args.as_of else None
    rule_hash = current_rule_hash()

    if args.record:
        with get_sync_session_factory()() as session:
            cells, context = run_promotion(session, layers=layers, sources=sources, as_of=as_of, rule_hash=rule_hash)
            context['recorded'] = record_cells(session, cells, rule_hash=rule_hash, code_version=current_code_version())
            session.commit()
    else:
        with readonly_session(get_sync_engine()) as session:
            cells, context = run_promotion(session, layers=layers, sources=sources, as_of=as_of, rule_hash=rule_hash)

    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    if args.json:
        print(json.dumps({'context': context, 'cells': [cell_to_jsonable(c) for c in cells]}, indent=2, default=str))
    else:
        print(format_text(cells, context))
        if args.record:
            print(f"recorded {context['recorded']} promotion_decision rows")
    return 0


if __name__ == '__main__':
    sys.exit(main())
