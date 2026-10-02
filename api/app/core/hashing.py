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

import hashlib
import math
import subprocess
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from app.core import constants as C
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


def rule_hash_input(constants: Mapping[str, Any] | None = None, rule_version: str | None = None) -> str:
    """The exact text that :func:`rule_hash` digests (exposed for tests/audit)."""
    payload = {
        'rule_version': C.RULE_VERSION if rule_version is None else rule_version,
        'constants': frozen_constants() if constants is None else constants,
    }
    return canonical_serialize(payload)


def rule_hash(constants: Mapping[str, Any] | None = None, rule_version: str | None = None) -> str:
    """Hex digest over the frozen constants plus the rule-version string.

    Derivation: ``sha256(canonical_serialize({"rule_version": RULE_VERSION,
    "constants": {NAME: value, ...}}))`` where the constants are the live
    public UPPERCASE values of ``app.core.constants``. It hashes VALUES, never
    file bytes, so a comment or formatting edit does not change it, while any
    threshold change (or a ``RULE_VERSION`` bump) does. Arguments exist so
    tests can show the sensitivity without editing the module; production
    callers pass nothing.
    """
    return _digest(rule_hash_input(constants, rule_version))


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
