"""Stats cache single-flight and one-entry-per-source."""

from __future__ import annotations

import threading
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.routers import stats_cache
from app.routers.stats_cache import clear_stats_cache, get_cached_stats


def test_cache_keeps_one_entry_per_source(session, db_engine, db_schema, monkeypatch):
    clear_stats_cache()
    calls = {'n': 0}

    def slow_run_stats(sess, *args, **kwargs):
        calls['n'] += 1
        time.sleep(0.08)
        from app.worker import stats_job

        return stats_job.run_stats(sess, *args, **kwargs)

    monkeypatch.setattr(stats_cache, 'run_stats', slow_run_stats)

    def open_session() -> tuple[Session, object]:
        connection = db_engine.connect()
        connection.execute(text(f'SET search_path TO "{db_schema}"'))
        return Session(bind=connection, join_transaction_mode='create_savepoint'), connection

    barrier = threading.Barrier(2)
    results: list = []
    errors: list[BaseException] = []

    def worker():
        sess, conn = open_session()
        try:
            barrier.wait(timeout=5)
            results.append(get_cached_stats(sess, 'replay'))
        except BaseException as exc:
            errors.append(exc)
        finally:
            sess.close()
            conn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors, errors
    assert calls['n'] == 1
    assert len(results) == 2
    assert results[0].cache_key == results[1].cache_key
    assert len(stats_cache._cache_by_source) == 1
