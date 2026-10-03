"""The three hashes every label row carries: feature hash, rule hash, code version.

Determinism is the whole requirement. Output must be identical across runs,
processes, machines and months, so:

* serialization is a hand-written canonical text form, not ``repr``/``str``
  of containers and not ``json.dumps`` defaults;
* every supported type has an explicit rule; an unsupported type RAISES
  ``TypeError`` instead of falling back to ``str()``.

Canonical form (``canonical_serialize``)
----------------------------------------
``None``     -> ``null``
``bool``     -> ``true`` / ``false``        (checked before ``int``)
``int``      -> decimal digits, e.g. ``-3``
``float``    -> ``repr(float)``, the shortest text that round-trips the IEEE
                double, e.g. ``0.1``, ``1e-07``, ``1.0``. ``-0.0`` becomes
                ``0.0``. ``nan`` / ``inf`` / ``-inf`` are written literally.
                ``0.1`` and ``0.10`` are the same double, hence the same text.
``Decimal``  -> ``D"<plain digits>"``: normalized, no exponent, so
                ``Decimal('1.50') == Decimal('1.5')`` and ``Decimal('100')``
                hashes as ``100``. Non-finite Decimals are rejected.
``str``      -> JSON-escaped, ASCII-only string literal
``datetime`` -> ``T"<UTC ISO-8601 with microseconds>"``; naive datetimes rejected
``date``     -> ``d"YYYY-MM-DD"``
``Mapping``  -> ``{key:value,...}`` with keys (``str`` only) sorted
``list`` / ``tuple`` -> ``[a,b,...]`` in order (a tuple and a list are equal)
numpy scalars -> converted to the equivalent Python bool/int/float

``int`` 1, ``float`` 1.0 and ``Decimal`` 1 are deliberately DIFFERENT
(``1``, ``1.0``, ``D"1"``): the type is part of the fact. ``set`` and
``frozenset`` are rejected (no defined order contract).
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import subprocess
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from app.core import constants as C
from app.core.features import params as P
from app.core.time import ensure_utc

CODE_VERSION_UNKNOWN = 'unknown'
DIRTY_SUFFIX = '-dirty'

# repo/api/app/core/hashing.py -> parents: core, app, api, repo
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------
def _quote(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append(f'\\u{code:04x}')
        else:  # astral plane: UTF-16 surrogate pair, like JSON
            code -= 0x10000
            out.append(f'\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}')
    out.append('"')
    return ''.join(out)


def _float_text(value: float) -> str:
    if math.isnan(value):
        return 'nan'
    if math.isinf(value):
        return 'inf' if value > 0 else '-inf'
    if value == 0.0:
        return '0.0'  # folds -0.0 into 0.0
    return repr(value)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise TypeError(f'non-finite Decimal {value!r} has no canonical form')
    if value == 0:
        return 'D"0"'
    return f'D"{format(value.normalize(), "f")}"'


def canonical_serialize(value: Any) -> str:
    """Canonical text for ``value``. Raises ``TypeError`` for undefined types."""
    if value is None:
        return 'null'
    if isinstance(value, (bool, np.bool_)):
        return 'true' if bool(value) else 'false'
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return _float_text(float(value))
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        return f'T{_quote(ensure_utc(value).isoformat(timespec="microseconds"))}'
    if isinstance(value, date):
        return f'd{_quote(value.isoformat())}'
    if isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f'mapping keys must be str, got {type(key).__name__}')
        for key in sorted(value):
            items.append(f'{_quote(key)}:{canonical_serialize(value[key])}')
        return '{' + ','.join(items) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(canonical_serialize(item) for item in value) + ']'
    raise TypeError(f'no canonical serialization rule for type {type(value).__name__}')


def _digest(text: str) -> str:
    return hashlib.new(C.HASH_ALGORITHM, text.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# feature_hash
# ---------------------------------------------------------------------------
def feature_hash(features: Mapping[str, Any]) -> str:
    """Hex digest (sha256, 64 chars) of a feature mapping.

    Key order is irrelevant; values follow the canonical rules above. Raises
    ``TypeError`` for a non-mapping input, a non-``str`` key, or any value of
    a type with no defined rule.
    """
    if not isinstance(features, Mapping):
        raise TypeError(f'features must be a mapping, got {type(features).__name__}')
    return _digest(canonical_serialize(features))


# ---------------------------------------------------------------------------
# rule_hash
# ---------------------------------------------------------------------------
def frozen_constants() -> dict[str, Any]:
    """The frozen set: public UPPERCASE names in ``constants.py`` except ``RULE_VERSION``.

    Names starting with ``_`` and anything not fully uppercase (imports,
    helpers) are excluded. Comments and docstrings are not values, so editing
    them cannot change the rule hash.
    """
    return {
        name: value
        for name, value in vars(C).items()
        if name.isupper() and not name.startswith('_') and name != 'RULE_VERSION'
    }


def _whole_seconds(value: timedelta) -> int:
    if value % timedelta(seconds=1):
        raise ValueError(f'{value!r} is not a whole number of seconds')
    return value // timedelta(seconds=1)


def frozen_engine_params() -> dict[str, Any]:
    """The public UPPERCASE values of ``app.core.features.params``, in serializable form.

    Every name is listed explicitly so that a new parameter cannot slip in
    unhashed or with an undefined value type; ``tests/test_hashing.py`` fails
    if ``params.py`` gains a public UPPERCASE name that is missing here. Two
    values are converted: ``FUNDING_MAX_AGE`` (a ``timedelta``) is stored as
    ``FUNDING_MAX_AGE_SECONDS`` (``int``), and ``PRODUCTION_POLICY`` (a
    ``HistoryPolicy``) as a mapping of its dataclass fields.
    """
    policy = P.PRODUCTION_POLICY
    return {
        'SIGNAL_TIMEFRAME': P.SIGNAL_TIMEFRAME,
        'ATR_TIMEFRAME': P.ATR_TIMEFRAME,
        'REALIZED_VOL_TIMEFRAME': P.REALIZED_VOL_TIMEFRAME,
        'REALIZED_VOL_BARS': P.REALIZED_VOL_BARS,
        'BASIS_TIMEFRAME': P.BASIS_TIMEFRAME,
        'OI_SERIES_TIMEFRAME': P.OI_SERIES_TIMEFRAME,
        'OI_CHANGE_STEPS': P.OI_CHANGE_STEPS,
        'OI_MAX_AGE_PERIODS': P.OI_MAX_AGE_PERIODS,
        'FUNDING_MAX_AGE_SECONDS': _whole_seconds(P.FUNDING_MAX_AGE),
        'HISTORY_DAYS': P.HISTORY_DAYS,
        'PRODUCTION_POLICY': {field.name: getattr(policy, field.name) for field in dataclasses.fields(policy)},
        'ATR_HISTORY_BARS': P.ATR_HISTORY_BARS,
        'RSI_HISTORY_BARS': P.RSI_HISTORY_BARS,
        'SWING_HISTORY_BARS': P.SWING_HISTORY_BARS,
        'FAMILY_MEMORY_BARS': P.FAMILY_MEMORY_BARS,
        'SESSION_MIN_BARS': P.SESSION_MIN_BARS,
        'LIQUIDATION_LOOKBACK_HOURS': P.LIQUIDATION_LOOKBACK_HOURS,
        'LIQUIDATION_MIN_PRINTS': P.LIQUIDATION_MIN_PRINTS,
        'LIQUIDATION_BIN_ATR': P.LIQUIDATION_BIN_ATR,
        'LIQUIDATION_RATE_WINDOW_MINUTES': P.LIQUIDATION_RATE_WINDOW_MINUTES,
        'DAYS_PER_YEAR': P.DAYS_PER_YEAR,
        'HOURS_PER_YEAR': P.HOURS_PER_YEAR,
    }


def rule_hash_input(
    constants: Mapping[str, Any] | None = None,
    rule_version: str | None = None,
    engine_params: Mapping[str, Any] | None = None,
) -> str:
    """The exact text that :func:`rule_hash` digests (exposed for tests/audit)."""
    payload = {
        'rule_version': C.RULE_VERSION if rule_version is None else rule_version,
        'constants': frozen_constants() if constants is None else constants,
        'engine_params': frozen_engine_params() if engine_params is None else engine_params,
    }
    return canonical_serialize(payload)


def rule_hash(
    constants: Mapping[str, Any] | None = None,
    rule_version: str | None = None,
    engine_params: Mapping[str, Any] | None = None,
) -> str:
    """Hex digest over the frozen constants, the engine parameters and the rule-version string.

    Derivation: ``sha256(canonical_serialize({"rule_version": RULE_VERSION,
    "constants": {NAME: value, ...}, "engine_params": {NAME: value, ...}}))``
    where the constants are the live public UPPERCASE values of
    ``app.core.constants`` and the engine parameters are
    :func:`frozen_engine_params`. It hashes VALUES, never file bytes, so a
    comment or formatting edit does not change it, while any threshold or
    engine-parameter change (or a ``RULE_VERSION`` bump) does. Infrastructure
    settings in ``app.config`` (publication lags, settle margin, watchlist) are
    NOT part of it. Arguments exist so tests can show the sensitivity without
    editing the module; production callers pass nothing.
    """
    return _digest(rule_hash_input(constants, rule_version, engine_params))


# ---------------------------------------------------------------------------
# code_version
# ---------------------------------------------------------------------------
def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def code_version() -> str:
    """Short git sha of the repo plus ``-dirty`` when the worktree has changes.

    Untracked files count as dirty. Returns ``'unknown'`` (never raises) when
    git is missing, the directory is not a repo, or git fails.
    """
    sha = _git('rev-parse', '--short', 'HEAD')
    if sha is None or not sha.strip():
        return CODE_VERSION_UNKNOWN
    status = _git('status', '--porcelain')
    if status is None:
        return f'{sha.strip()}{DIRTY_SUFFIX}'  # cannot prove clean -> treat as dirty
    return sha.strip() + (DIRTY_SUFFIX if status.strip() else '')
