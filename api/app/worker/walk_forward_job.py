"""Walk-forward dry run: print the purged folds a Phase 2 model would be judged in. Read-only; fits nothing.

``python -m app.worker.walk_forward_job [--source live|replay] [--horizon scalp|swing|position] [--json]``

Loads labeled candidates on the current `rule_hash` with the stats job's loader, splits them by cohort, builds one fold set per
cohort and horizon (`app.core.learning.walk_forward.build_folds`) and prints the fold counts and whether the label floor would
allow a fit. The session is READ ONLY at the database level, exactly as the stats job opens it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.learning.walk_forward import Fold, FoldSet, build_folds
from app.core.stats.observations import split_by_cohort
from app.models import enums as E
from app.worker.stats_job import load_observations, readonly_session


def run_walk_forward(session: Session, *, source: str | None = None, horizon: str | None = None) -> list[FoldSet]:
    """One fold set per (cohort, horizon) present in the labeled data, cohorts in sort order, horizons in spec order."""
    loaded = load_observations(session, source=source)
    out: list[FoldSet] = []
    for cohort, rows in split_by_cohort(loaded.observations).items():
        for h in C.HORIZONS:
            if horizon is not None and h != horizon:
                continue
            subset = [o for o in rows if o.horizon == h]
            if subset:
                out.append(build_folds(subset, cohort, h))
    return out


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def fold_to_dict(fold: Fold) -> dict[str, Any]:
    train_range, test_range = fold.train_range, fold.test_range
    return {
        'index': fold.index,
        'test_start': _iso(fold.test_start),
        'test_end': _iso(fold.test_end),
        'n_train': fold.n_train,
        'n_test': fold.n_test,
        'train_range': None if train_range is None else [_iso(t) for t in train_range],
        'test_range': None if test_range is None else [_iso(t) for t in test_range],
        'test_locked_book': len(fold.test_locked_book),
        'test_event_window': len(fold.test_event_window),
        'outside_train_window': fold.outside_train_window,
        'excluded_locked_book': fold.excluded_locked_book,
        'excluded_event_window': fold.excluded_event_window,
        'purged': fold.purged,
        'embargoed': fold.embargoed,
        'fit_allowed': fold.fit_allowed,
    }


def fold_set_to_dict(fold_set: FoldSet) -> dict[str, Any]:
    return {
        'cohort': fold_set.cohort.label,
        'horizon': fold_set.horizon,
        'embargo_hours': fold_set.embargo.total_seconds() / 3600,
        'test_window_days': fold_set.test_window.days,
        'min_train': fold_set.min_train,
        'max_train_days': fold_set.max_train_days,
        'n_raw': fold_set.n_raw,
        'n_independent': fold_set.n_independent,
        'n_no_entry': fold_set.n_no_entry,
        'n_samples': fold_set.n_samples,
        'n_locked_book': fold_set.n_locked_book,
        'n_event_window': fold_set.n_event_window,
        'n_trainable': fold_set.n_trainable,
        'floor': fold_set.floor,
        'fit_allowed': fold_set.fit_allowed,
        'grid_origin': _iso(fold_set.grid_origin),
        'windows_below_min_train': fold_set.windows_below_min_train,
        'folds': [fold_to_dict(f) for f in fold_set.folds],
    }


def _short(value: Any) -> str:
    return '-' if value is None else value.strftime('%Y-%m-%d %H:%M')


def format_text(fold_sets: Sequence[FoldSet]) -> str:
    lines = ['Walk-forward dry run (read-only; nothing is fitted)']
    if not fold_sets:
        lines.append('no labeled observations on the current rule_hash')
        return '\n'.join(lines)
    first = fold_sets[0]
    lines.append(
        f'embargo {first.embargo.total_seconds() / 3600:g}h, test window {first.test_window.days}d, '
        f'min train {first.min_train}, training window {"expanding" if first.max_train_days is None else f"{first.max_train_days}d"}'
    )
    for fs in fold_sets:
        lines.append('')
        lines.append(f'== {fs.cohort.label} / {fs.horizon}')
        lines.append(
            f'raw {fs.n_raw}, independent {fs.n_independent}, no_entry {fs.n_no_entry}, samples {fs.n_samples}, '
            f'locked book {fs.n_locked_book}, event window {fs.n_event_window}, trainable {fs.n_trainable}'
        )
        lines.append(f'fit allowed: {"yes" if fs.fit_allowed else "no"} (n={fs.n_trainable}/{fs.floor})')
        lines.append(f'folds {len(fs.folds)}, windows skipped below min train {fs.windows_below_min_train}')
        for f in fs.folds:
            lines.append(
                f'  fold {f.index}: test [{_short(f.test_start)}, {_short(f.test_end)}) n={f.n_test} '
                f'(locked {len(f.test_locked_book)}, event {len(f.test_event_window)}); '
                f'train n={f.n_train} {_short(f.train_range[0]) if f.train_range else "-"} .. '
                f'{_short(f.train_range[1]) if f.train_range else "-"}; '
                f'purged {f.purged}, embargoed {f.embargoed}, excl locked {f.excluded_locked_book}, '
                f'excl event {f.excluded_event_window}, outside window {f.outside_train_window}; '
                f'fit allowed {"yes" if f.fit_allowed else "no"}'
            )
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.db_sync import get_sync_engine

    parser = argparse.ArgumentParser(description='Walk-forward fold dry run over labeled candidates (read-only, fits nothing).')
    parser.add_argument('--source', choices=E.SOURCES)
    parser.add_argument('--horizon', choices=C.HORIZONS)
    parser.add_argument('--json', action='store_true', help='print JSON instead of text')
    args = parser.parse_args(argv)

    with readonly_session(get_sync_engine()) as session:
        fold_sets = run_walk_forward(session, source=args.source, horizon=args.horizon)
    if args.json:
        text = json.dumps([fold_set_to_dict(fs) for fs in fold_sets], indent=2)
    else:
        text = format_text(fold_sets)
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
