"""Worker: exchange ingest, backfill and the live scheduler.

``python -m app.worker`` runs the scheduler; ``python -m app.worker.backfill``
bootstraps history. The worker moves bytes from exchanges into tables, correctly
timestamped, and does nothing else: no feature math, no rules, no labels.
"""
