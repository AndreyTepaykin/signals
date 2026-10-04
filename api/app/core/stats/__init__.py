"""Milestone 8 stats engine: pure functions over labeled candidates.

``params``        stats-only parameters (not hashed)
``observations``  `Observation`, `CohortKey` (source x options path x lookback), the classification functions
``summary``       independent sample (`collapse_independent`), expectancy, interval, profit factor, drawdown, Brier
``reliability``   reliability diagram dataset (bins under 20 outcomes hidden)
``report``        walk-up, baselines, kill rule, the assembled report
``render``        text and JSON output
"""
