"""A value that may legitimately be missing, with the reason it is missing.

Every feature function that can fail to produce a number returns a
:class:`Reading` instead of ``None`` or ``0.0``. ``value`` is ``None`` exactly
when the feature is unavailable and ``reason`` then says why in one sentence
(the real-data report quotes them). There is no way to build a Reading that is
both a value and a reason, and no way to spell "missing" as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Reading:
    value: Any
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.reason:
            raise ValueError('a missing Reading must carry a reason')
        if self.value is not None and self.reason is not None:
            raise ValueError('a Reading with a value cannot also carry a reason')
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError(f'a Reading may not hold a non-finite float ({self.value!r}); return missing instead')

    @property
    def ok(self) -> bool:
        return self.value is not None


def have(value: Any) -> Reading:
    return Reading(value)


def missing(reason: str) -> Reading:
    return Reading(None, reason)
