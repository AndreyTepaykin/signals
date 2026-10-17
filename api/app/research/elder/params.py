"""Parameters of the Elder Triple Screen research.

Two groups live here:

* Fixed constants. They are part of the method, never tuned, never in
  ``params.json``.
* :class:`ElderParams`, the twelve values the E6 tuner proposes. This class
  checks types, enum values and basic sanity only. Numeric bounds for tuning
  belong to ``params_space.json`` (E5), not here.

``wave_threshold`` has different units for each ``wave_osc``; see
:mod:`app.research.elder.signals`:

* ``force2``: Force Index(2) divided by (ATR x SMA20 of volume), a pure number.
* ``stoch``: points of slow %K, in ``[0, 100]``.
* ``elder_ray``: multiples of the 6H ATR.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

T2_R = 10.0
EMA_PERIOD = 13
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD = 14
STOCH_PERIOD = 5
STOCH_SMOOTH = 3
FORCE_PERIOD = 2
VOLUME_SMA = 20
COST_ROUND_TRIP = 0.0012
MAX_COST_R = 0.1

TIDE_MODES = ('ema', 'macd', 'both')
WAVE_OSCS = ('force2', 'stoch', 'elder_ray')

_STR_FIELDS = ('tide_mode', 'wave_osc')
_FLOAT_FIELDS = (
    'wave_threshold',
    'stop_atr_buffer',
    'min_stop_atr',
    't1_r',
    't1_share',
    'trail_atr',
)
_INT_FIELDS = ('ripple_expiry_bars', 'stop_lookback_bars')
_BOOL_FIELDS = ('wave_block_impulse', 'be_after_t1')


@dataclass(frozen=True)
class ElderParams:
    """Tunable parameters. Every violation raises ``ValueError``.

    ``int`` values given for float fields are converted to ``float`` so that
    ``params_hash`` does not depend on whether a JSON file wrote ``1`` or ``1.0``.
    """

    tide_mode: str
    wave_osc: str
    wave_threshold: float
    wave_block_impulse: bool
    ripple_expiry_bars: int
    stop_lookback_bars: int
    stop_atr_buffer: float
    min_stop_atr: float
    t1_r: float
    t1_share: float
    be_after_t1: bool
    trail_atr: float

    def __post_init__(self) -> None:
        for name in _STR_FIELDS:
            if not isinstance(getattr(self, name), str):
                raise ValueError(f'{name} must be a string')
        if self.tide_mode not in TIDE_MODES:
            raise ValueError(f'tide_mode must be one of {TIDE_MODES}, got {self.tide_mode!r}')
        if self.wave_osc not in WAVE_OSCS:
            raise ValueError(f'wave_osc must be one of {WAVE_OSCS}, got {self.wave_osc!r}')
        for name in _BOOL_FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f'{name} must be a bool')
        for name in _INT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f'{name} must be an int')
            if value < 1:
                raise ValueError(f'{name} must be >= 1, got {value}')
        for name in _FLOAT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f'{name} must be a number')
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
            if value < 0.0:
                raise ValueError(f'{name} must be >= 0, got {value}')
            object.__setattr__(self, name, value)
        if not 1.0 <= self.t1_r <= 2.0:
            raise ValueError(f't1_r must be in [1, 2], got {self.t1_r}')
        if not 0.0 <= self.t1_share <= 1.0:
            raise ValueError(f't1_share must be in [0, 1], got {self.t1_share}')
        if self.wave_osc == 'stoch' and not 0.0 <= self.wave_threshold <= 100.0:
            raise ValueError(
                f'wave_threshold must be in [0, 100] for stoch, got {self.wave_threshold}'
            )

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'ElderParams':
        """Build from a mapping with exactly the twelve field names."""
        if not isinstance(d, Mapping):
            raise ValueError('params must be a mapping')
        names = [f.name for f in fields(cls)]
        unknown = sorted(str(key) for key in d if key not in names)
        missing = [name for name in names if name not in d]
        if unknown:
            raise ValueError(f'unknown params keys: {", ".join(unknown)}')
        if missing:
            raise ValueError(f'missing params keys: {", ".join(missing)}')
        return cls(**{name: d[name] for name in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        """sha256 hex of the canonical JSON (sorted keys, no whitespace)."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
