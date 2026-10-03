"""The frozen rule layer (Milestone 4): pure functions from a feature row to a candidate decision.

Nothing in this package touches the database, the clock or the environment.
The worker (``app.worker.rules_job``) does the I/O: it loads the feature row
and the earlier candidates, calls these functions, and writes the result. A live
run and a replay run therefore apply byte-identical logic to the same inputs.

Modules
-------
``inputs``       the slice of a ``feature_row`` the rules read (``RuleInputs``).
``direction``    the direction vote (swing break supplies the side; positioning votes).
``triggers``     which triggers fire for a family and which setup family they map to.
``stops``        the chandelier stop as 1R and the pre-options target estimate.
``locked_book``  the 1-in-5 deterministic bucket.
``sample_group`` ``new`` / ``overlap`` / ``btc_impulse`` assignment and the one-open-signal test.
``evaluate``     the whole ladder: ``evaluate(inputs, context) -> Evaluation``.
"""
