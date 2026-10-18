"""``python -m app.research.elder.run`` — train trials, freeze, one holdout.

Train appends one line to ``trials.jsonl`` and never rewrites earlier lines.
The line stores ``prev_sha256``, the sha256 of the previous line's bytes
(including its trailing newline); the first line stores null. A broken chain
refuses the append (exit 3).

Holdout is not a trial. It needs ``frozen.json`` whose params hash is already
in ``trials.jsonl``. The first run writes ``holdout.lock``. A later run refuses
unless ``--force`` and a non-empty ``--reason``, which are appended to
``holdout_log.jsonl``. Nothing holdout does is written to ``trials.jsonl``.

Exit codes: 0 ok, 2 bad arguments or params outside ``params_space.json``,
3 a refusal (broken chain, freeze overwrite, holdout lock, hash mismatch).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.db_sync import dispose_sync_engine, get_sync_engine
from app.research.elder.load import (
    COINS,
    SUBWINDOWS,
    TRAIN_END,
    TRAIN_START,
    CoinData,
    latest_common_end,
    load_coin,
    readonly_connection,
)
from app.research.elder.params import ElderParams
from app.research.elder.report import build_report, render_markdown, render_side_by_side

UTC = timezone.utc
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

API_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT_DIR = API_ROOT / 'research_out' / 'elder'
SPACE_PATH = Path(__file__).resolve().with_name('params_space.json')

_PARAM_NAMES = (
    'tide_mode',
    'wave_osc',
    'wave_threshold',
    'wave_block_impulse',
    'ripple_expiry_bars',
    'stop_lookback_bars',
    'stop_atr_buffer',
    'min_stop_atr',
    't1_r',
    't1_share',
    'be_after_t1',
    'trail_atr',
)


class ChainError(ValueError):
    """``trials.jsonl`` is missing a link or a trial id."""


def _iso(instant: datetime) -> str:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return instant.astimezone(UTC).isoformat()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def _write_bytes(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.encode('utf-8'))


def _error(message: str) -> None:
    print(f'error: {message}', file=sys.stderr)


def parse_instant(text: str) -> datetime:
    """ISO date or datetime. A value without a zone is UTC."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'{text!r} is not an ISO date or datetime') from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def git_head() -> str | None:
    """Short git hash, or None when git is unavailable."""
    try:
        proc = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _check_range(name: str, value: float, bounds: Sequence[float], extra: str = '') -> str | None:
    lo, hi = float(bounds[0]), float(bounds[1])
    if lo <= value <= hi:
        return None
    return f'{name} {value} is outside [{lo}, {hi}]{extra}'


def _check_spec(name: str, value: Any, spec: Mapping[str, Any], params: ElderParams) -> str | None:
    if 'choices' in spec:
        if value not in spec['choices']:
            return f'{name} {value!r} is not one of {list(spec["choices"])}'
        return None
    if 'by_wave_osc' in spec:
        bounds = spec['by_wave_osc'][params.wave_osc]
        return _check_range(name, value, bounds, extra=f' for wave_osc {params.wave_osc}')
    if 'int' in spec:
        return _check_range(name, int(value), spec['int'])
    if 'range' in spec:
        return _check_range(name, value, spec['range'])
    return f'{name}: params_space.json has no bounds'


def validate_params(data: Mapping[str, Any], space: Mapping[str, Any]) -> ElderParams:
    """:meth:`ElderParams.from_dict` plus the bounds in ``params_space.json``."""
    params = ElderParams.from_dict(data)
    errors = []
    for name in _PARAM_NAMES:
        spec = space.get(name)
        if not isinstance(spec, Mapping):
            errors.append(f'{name}: missing from params_space.json')
            continue
        message = _check_spec(name, getattr(params, name), spec, params)
        if message:
            errors.append(message)
    if errors:
        raise ValueError('; '.join(errors))
    return params


def load_params(path: str | Path, space_path: Path = SPACE_PATH) -> ElderParams:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    space = json.loads(space_path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('params file must be a JSON object')
    if not isinstance(space, dict):
        raise ValueError('params_space.json must be a JSON object')
    return validate_params(data, space)


def load_research_data(period_end: datetime) -> list[CoinData]:
    """All research coins, read-only, truncated at ``period_end``.

    Tests replace this. The train command never asks for an end past 2026-07-01.
    """
    engine = get_sync_engine()
    with readonly_connection(engine) as conn:
        return [load_coin(conn, coin, period_end) for coin in COINS]


def _read_lines(path: Path) -> list[bytes]:
    if not path.exists():
        return []
    data = path.read_bytes()
    if not data:
        return []
    if not data.endswith(b'\n'):
        raise ChainError(f'{path.name} does not end with a newline')
    return data.splitlines(keepends=True)


def verify_trials(lines: Sequence[bytes]) -> None:
    """Each line's ``prev_sha256`` is the sha256 of the previous line's bytes."""
    previous: str | None = None
    for index, raw in enumerate(lines):
        if not raw.endswith(b'\n'):
            raise ChainError(f'trials.jsonl line {index + 1} has no newline')
        try:
            obj = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChainError(f'trials.jsonl line {index + 1} is not JSON') from exc
        if not isinstance(obj, dict):
            raise ChainError(f'trials.jsonl line {index + 1} is not an object')
        if obj.get('trial_id') != index + 1:
            raise ChainError(f'trials.jsonl line {index + 1} has trial_id {obj.get("trial_id")!r}, expected {index + 1}')
        if obj.get('prev_sha256') != previous:
            raise ChainError(f'trials.jsonl chain is broken at line {index + 1}')
        previous = hashlib.sha256(raw).hexdigest()


def _line_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'


def append_trial(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one train line. Refuses a broken chain. Does not rewrite earlier bytes."""
    lines = _read_lines(path)
    verify_trials(lines)
    expected_id = len(lines) + 1
    if record.get('trial_id') not in (None, expected_id):
        raise ChainError(f'trial_id {record.get("trial_id")!r} != {expected_id}')
    previous = None if not lines else hashlib.sha256(lines[-1]).hexdigest()
    payload = dict(record)
    payload['trial_id'] = expected_id
    payload['prev_sha256'] = previous
    blob = _line_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as handle:
        handle.write(blob)
    return payload


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    blob = _line_bytes(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as handle:
        handle.write(blob)


def _hash_in_train_trials(lines: Sequence[bytes], params_hash: str) -> bool:
    for raw in lines:
        obj = json.loads(raw.decode('utf-8'))
        if obj.get('period') == 'train' and obj.get('params_hash') == params_hash:
            return True
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m app.research.elder.run',
        description='Elder Triple Screen research runner. Train appends trials.jsonl. Holdout is locked.',
    )
    parser.add_argument('--period', choices=('train', 'holdout'))
    parser.add_argument('--params', help='params JSON (twelve ElderParams fields)')
    parser.add_argument('--out-dir', help=f'output directory (default: {DEFAULT_OUT_DIR})')
    parser.add_argument('--end', type=parse_instant, help='period end, UTC, exclusive. Train cannot pass 2026-07-01.')
    parser.add_argument('--freeze', action='store_true', help='write frozen.json from a train trial')
    parser.add_argument('--trial', type=int, help='trial id to freeze')
    parser.add_argument('--force', action='store_true', help='with --reason, run holdout again after the lock')
    parser.add_argument('--reason', default='', help='why a locked holdout is being run again')
    return parser


def _out_dir(args: argparse.Namespace) -> Path:
    return Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR


def _write_report(path: Path, report: Mapping[str, Any], markdown: str) -> None:
    _write_bytes(path, json.dumps(report, indent=2) + '\n')
    _write_bytes(path.with_suffix('.md'), markdown)


def _trial_record(report: Mapping[str, Any], params: ElderParams, report_name: str, start: datetime, end: datetime) -> dict[str, Any]:
    overall = report['overall']
    return {
        'utc': _now(),
        'params': params.to_dict(),
        'params_hash': params.params_hash(),
        'period': 'train',
        'start': _iso(start),
        'end': _iso(end),
        'git_head': report.get('git_head'),
        'objective': report['objective'],
        'objective_feasible': report['objective_feasible'],
        'roi': overall['roi'],
        'roi_interval': overall['roi_interval'],
        'max_drawdown_equity': overall['max_drawdown_equity'],
        'e_r': overall['e_r'],
        'e_r_interval': overall['e_r_interval'],
        'n_closed': overall['n_closed'],
        'n_censored': overall['n_censored'],
        'trades_per_week': overall['trades_per_week'],
        'p_t1': overall['p_t1']['p'],
        'p_t2': overall['p_t2']['p'],
        'report': report_name,
    }


def _cmd_train(args: argparse.Namespace) -> int:
    if args.freeze or args.trial is not None:
        _error('--period train does not take --freeze or --trial')
        return EXIT_USAGE
    if args.force or args.reason.strip():
        _error('--force and --reason are holdout-only')
        return EXIT_USAGE
    if not args.params:
        _error('--period train requires --params')
        return EXIT_USAGE
    end = args.end or TRAIN_END
    if end > TRAIN_END:
        _error('train --end cannot pass 2026-07-01; holdout is a separate command')
        return EXIT_USAGE
    if end <= TRAIN_START:
        _error('train end must be after 2026-01-01')
        return EXIT_USAGE
    try:
        params = load_params(args.params)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _error(str(exc))
        return EXIT_USAGE
    out = _out_dir(args)
    trials = out / 'trials.jsonl'
    try:
        lines = _read_lines(trials)
        verify_trials(lines)
    except ChainError as exc:
        _error(str(exc))
        return EXIT_REFUSED
    started = time.perf_counter()
    # Train loads only through `end`, which is at most 2026-07-01. Holdout bars are not read.
    data = load_research_data(end)
    report = build_report(data, params, TRAIN_START, end, subwindows=SUBWINDOWS)
    elapsed = time.perf_counter() - started
    report['runtime_s'] = elapsed
    report['git_head'] = git_head()
    trial_id = len(lines) + 1
    name = f'train_{trial_id:03d}_{params.params_hash()[:8]}.json'
    _write_report(out / name, report, render_markdown(report))
    try:
        append_trial(trials, _trial_record(report, params, name, TRAIN_START, end))
    except ChainError as exc:
        _error(str(exc))
        return EXIT_REFUSED
    print(render_markdown(report), end='')
    print(f'runtime_s {elapsed:.2f}')
    print(f'wrote {name}')
    return EXIT_OK


def _cmd_freeze(args: argparse.Namespace) -> int:
    if args.period or args.params or args.force or args.reason.strip() or args.end is not None:
        _error('--freeze only takes --trial and --out-dir')
        return EXIT_USAGE
    if args.trial is None or args.trial < 1:
        _error('--freeze requires --trial N with N >= 1')
        return EXIT_USAGE
    out = _out_dir(args)
    frozen = out / 'frozen.json'
    if frozen.exists():
        _error(f'{frozen} already exists; refusing to overwrite')
        return EXIT_REFUSED
    try:
        lines = _read_lines(out / 'trials.jsonl')
        verify_trials(lines)
    except ChainError as exc:
        _error(str(exc))
        return EXIT_REFUSED
    if args.trial > len(lines):
        _error(f'trial {args.trial} does not exist ({len(lines)} on file)')
        return EXIT_REFUSED
    obj = json.loads(lines[args.trial - 1].decode('utf-8'))
    if obj.get('period') != 'train':
        _error('only a train trial can be frozen')
        return EXIT_REFUSED
    try:
        params = ElderParams.from_dict(obj['params'])
    except (KeyError, TypeError, ValueError) as exc:
        _error(f'trial params are unreadable: {exc}')
        return EXIT_REFUSED
    if params.params_hash() != obj.get('params_hash'):
        _error('trial params_hash does not match its params')
        return EXIT_REFUSED
    _write_bytes(frozen, json.dumps(params.to_dict(), indent=2) + '\n')
    print(f'wrote {frozen.name} from trial {args.trial}')
    return EXIT_OK


def _cmd_holdout(args: argparse.Namespace) -> int:
    if args.freeze or args.trial is not None:
        _error('--period holdout does not take --freeze or --trial')
        return EXIT_USAGE
    if not args.params:
        _error('--period holdout requires --params')
        return EXIT_USAGE
    out = _out_dir(args)
    frozen_path = out / 'frozen.json'
    if not frozen_path.exists():
        _error(f'{frozen_path} does not exist')
        return EXIT_REFUSED
    try:
        params = load_params(args.params)
        frozen_params = ElderParams.from_dict(json.loads(frozen_path.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _error(str(exc))
        return EXIT_USAGE
    if params.params_hash() != frozen_params.params_hash():
        _error('params file does not match frozen.json')
        return EXIT_REFUSED
    trials = out / 'trials.jsonl'
    try:
        lines = _read_lines(trials)
        verify_trials(lines)
    except ChainError as exc:
        _error(str(exc))
        return EXIT_REFUSED
    if not _hash_in_train_trials(lines, params.params_hash()):
        _error('params_hash is not a train trial in trials.jsonl')
        return EXIT_REFUSED
    lock_path = out / 'holdout.lock'
    reason = args.reason.strip()
    if lock_path.exists():
        if not args.force or not reason:
            _error('holdout.lock exists; refusing without --force and a non-empty --reason')
            return EXIT_REFUSED
        forced = True
    else:
        forced = False
    if args.end is None:
        engine = get_sync_engine()
        with readonly_connection(engine) as conn:
            end = latest_common_end(conn, COINS)
    else:
        end = args.end
    if end <= TRAIN_END:
        _error('holdout end must be after 2026-07-01')
        return EXIT_USAGE
    trials_before = trials.read_bytes() if trials.exists() else b''
    started = time.perf_counter()
    train_data = load_research_data(TRAIN_END)
    holdout_data = load_research_data(end)
    train_report = build_report(train_data, params, TRAIN_START, TRAIN_END, subwindows=SUBWINDOWS)
    holdout_report = build_report(holdout_data, params, TRAIN_END, end, subwindows=())
    elapsed = time.perf_counter() - started
    payload = {
        'params': params.to_dict(),
        'params_hash': params.params_hash(),
        'end': _iso(end),
        'runtime_s': elapsed,
        'git_head': git_head(),
        'train': train_report,
        'holdout': holdout_report,
    }
    name = f'holdout_{params.params_hash()[:8]}.json'
    _write_report(out / name, payload, render_side_by_side(train_report, holdout_report))
    _append_jsonl(
        out / 'holdout_log.jsonl',
        {
            'params_hash': params.params_hash(),
            'utc': _now(),
            'end': _iso(end),
            'forced': forced,
            'reason': reason if forced else None,
        },
    )
    if not lock_path.exists():
        _write_bytes(
            lock_path,
            json.dumps({'params_hash': params.params_hash(), 'utc': _now(), 'end': _iso(end)}, indent=2) + '\n',
        )
    after = trials.read_bytes() if trials.exists() else b''
    if after != trials_before:
        _error('holdout must not modify trials.jsonl')
        return EXIT_REFUSED
    print(render_side_by_side(train_report, holdout_report), end='')
    print(f'runtime_s {elapsed:.2f}')
    print(f'wrote {name}')
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.freeze:
            return _cmd_freeze(args)
        if args.period == 'train':
            return _cmd_train(args)
        if args.period == 'holdout':
            return _cmd_holdout(args)
        _error('give --period train, --period holdout, or --freeze --trial N')
        return EXIT_USAGE
    finally:
        dispose_sync_engine()


if __name__ == '__main__':
    sys.exit(main())
