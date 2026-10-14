"""Walk-forward harness parameters (Phase 2 preparation).

Not hashed: nothing here decides a candidate, a label or an estimate. They only shape how a later model would be
evaluated. The label floor and the horizon durations are read from ``app.core.constants`` and never restated.
"""

from __future__ import annotations

#: Each fold tests one window of this many days; the next fold's window starts where this one ends (weekly slow path).
TEST_WINDOW_DAYS: int = 7
#: Test windows sit on a calendar grid that starts at 00:00 UTC on this weekday (0 = Monday) on or before the first
#: independent sample, so a rerun on more data keeps the earlier windows where they were.
ANCHOR_WEEKDAY: int = 0
#: A fold whose training set holds fewer independent samples than this is not built (counted as skipped).
MIN_TRAIN_SAMPLES: int = 30
#: Expanding training window by default; a number of days here turns it into a rolling window.
MAX_TRAIN_DAYS: int | None = None
