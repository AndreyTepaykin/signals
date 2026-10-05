"""In-process cache for expensive ``StatsReport`` builds."""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.hashing import rule_hash as current_rule_hash
from app.core.stats.render import to_jsonable
from app.core.stats.report import StatsReport
from app.core.time import utc_now
from app.models.decisions import Candidate
from app.models.labels import Label
from app.worker.stats_job import run_stats


@dataclasses.dataclass
class CachedStats:
    report: StatsReport
    jsonable: dict[str, Any]
    built_at: datetime
    cache_key: tuple[str, str, tuple[int, int | None, int | None, int]]


_cache_lock = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}
_cache_by_source: dict[str, CachedStats] = {}


def clear_stats_cache() -> None:
    with _cache_lock:
        _cache_by_source.clear()


def _build_lock_for(source: str) -> threading.Lock:
    with _cache_lock:
        lock = _build_locks.get(source)
        if lock is None:
            lock = threading.Lock()
            _build_locks[source] = lock
        return lock


def fingerprint(session: Session, source: str) -> tuple[int, int | None, int | None, int]:
    label_count = session.scalar(
        select(func.count())
        .select_from(Label)
        .join(Candidate, Candidate.id == Label.candidate_id)
        .where(Candidate.source == source)
    )
    max_label_id = session.scalar(
        select(func.max(Label.candidate_id))
        .join(Candidate, Candidate.id == Label.candidate_id)
        .where(Candidate.source == source)
    )
    max_candidate_id = session.scalar(select(func.max(Candidate.id)).where(Candidate.source == source))
    estimate_count = session.scalar(
        select(func.count()).select_from(Candidate).where(
            Candidate.source == source,
            Candidate.p_upper_before_stop.isnot(None),
        )
    )
    return (int(label_count or 0), max_label_id, max_candidate_id, int(estimate_count or 0))


def get_cached_stats(session: Session, source: str) -> CachedStats:
    rh = current_rule_hash()
    fp = fingerprint(session, source)
    key = (source, rh, fp)
    with _cache_lock:
        hit = _cache_by_source.get(source)
        if hit is not None and hit.cache_key == key:
            return hit
    build_lock = _build_lock_for(source)
    with build_lock:
        with _cache_lock:
            hit = _cache_by_source.get(source)
            if hit is not None and hit.cache_key == key:
                return hit
        report = run_stats(session, source=source, rule_hash=rh)
        built_at = utc_now()
        jsonable = to_jsonable(report)
        jsonable['built_at'] = built_at.isoformat()
        entry = CachedStats(report=report, jsonable=jsonable, built_at=built_at, cache_key=key)
        with _cache_lock:
            _cache_by_source[source] = entry
        return entry


def stats_envelope(cached: CachedStats) -> dict[str, Any]:
    return {
        'rule_hash': cached.report.rule_hash,
        'context': cached.report.context,
        'parameters': cached.report.parameters,
        'built_at': cached.built_at.isoformat(),
    }
