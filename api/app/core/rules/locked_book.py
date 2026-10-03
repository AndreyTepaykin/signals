"""The 1-in-5 locked book.

Spec: "One in five decision times, chosen by a hash of coin and bar time before the
outcome exists, goes to a locked book. That book never updates weights."

``locked_book_bucket(coin, bar_close)`` is the first ``LOCKED_BOOK_HASH_BYTES`` bytes
of ``HASH_ALGORITHM`` over the canonical text (``hashing.canonical_serialize``) of
``{"bar_close": <aware UTC instant>, "coin": <symbol>}``, read as a big-endian integer,
modulo ``LOCKED_BOOK_ONE_IN``. The candidate is in the locked book when the bucket
equals ``LOCKED_BOOK_SELECTED_BUCKET``.

It depends on the coin and the signal-bar close only - not on the horizon, the
direction, the status, the features or any outcome - so the same bar is locked (or
not) for every horizon and every status, and the answer is known before anything
resolves. The same function gives the same answer in a live run, in a replay and in
another process (no ``hash()``, no seed, no clock).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.core import constants as C
from app.core.hashing import canonical_serialize


def locked_book_text(coin: str, bar_close: datetime) -> str:
    """The canonical text that is hashed. ``bar_close`` must be timezone-aware."""
    return canonical_serialize({'coin': coin, 'bar_close': bar_close})


def locked_book_bucket(coin: str, bar_close: datetime) -> int:
    digest = hashlib.new(C.HASH_ALGORITHM, locked_book_text(coin, bar_close).encode('utf-8')).digest()
    return int.from_bytes(digest[: C.LOCKED_BOOK_HASH_BYTES], 'big') % C.LOCKED_BOOK_ONE_IN


def is_locked_book(coin: str, bar_close: datetime) -> bool:
    return locked_book_bucket(coin, bar_close) == C.LOCKED_BOOK_SELECTED_BUCKET
