"""Signal card endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.hashing import rule_hash as current_rule_hash
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.routers.deps import ReadonlySession, SourceQuery
from app.routers.serialize import (
    card_action,
    cost_line,
    drivers_from_feature,
    edge_hidden_block,
    fnum,
    vol_bucket,
)

router = APIRouter(prefix='/cards', tags=['cards'])


class OptionsLineOut(BaseModel):
    verdict: str | None
    venue: str | None
    vol_tag: str | None
    dealer_gamma_sign: int | None
    nearest_path_strike: float | None
    hours_to_expiry: float | None
    expression: str | None
    size_multiplier: float | None
    influence: float | None
    confidence: float | None
    snapshot_time: datetime | None


class EstimatesOut(BaseModel):
    p_upper_before_stop: float | None = None
    expected_r: float | None = None
    expected_mae_r: float | None = None
    median_hours_to_resolution: float | None = None
    pool_level: str | None = None
    pool_n: int | None = None
    expected_r_ci_low: float | None = None
    expected_r_ci_high: float | None = None
    pool_interval_confidence: float | None = None


class SignalCardOut(BaseModel):
    id: int
    action: str
    status: str
    status_reason: str | None
    coin: str
    horizon: str
    decision_time: datetime
    bar_close: datetime
    source: str
    locked_book: bool
    entry_price: float | None
    stop_price: float | None
    pre_options_stop_price: float | None
    upper_barrier_price: float | None
    lower_barrier_price: float | None
    target_cap_price: float | None
    one_r: float | None
    vertical_deadline: datetime | None
    edge_hidden: bool
    edge_hidden_reason: str | None
    estimates: EstimatesOut
    regime: dict[str, Any]
    options_line: OptionsLineOut
    trigger: str
    setup_family: str
    location: dict[str, Any]
    drivers: list[dict[str, Any]]
    cost_line: dict[str, Any]
    feature_hash: str
    rule_hash: str
    code_version: str
    on_current_rule_hash: bool


class CardListOut(BaseModel):
    items: list[SignalCardOut]
    total: int
    limit: int
    offset: int


def _serialize_card(candidate: Candidate, feature: FeatureRow | None) -> SignalCardOut:
    hidden, reason = edge_hidden_block(candidate)
    estimates = EstimatesOut()
    if not hidden:
        estimates = EstimatesOut(
            p_upper_before_stop=fnum(candidate.p_upper_before_stop),
            expected_r=fnum(candidate.expected_r),
            expected_mae_r=fnum(candidate.expected_mae_r),
            median_hours_to_resolution=fnum(candidate.median_hours_to_resolution),
            pool_level=candidate.pool_level,
            pool_n=candidate.pool_n,
            expected_r_ci_low=fnum(candidate.expected_r_ci_low),
            expected_r_ci_high=fnum(candidate.expected_r_ci_high),
            pool_interval_confidence=fnum(candidate.pool_interval_confidence),
        )
    vol_pct = fnum(feature.realized_vol_pct) if feature else None
    regime = {
        'trend_family': (feature.trend_family if feature and feature.trend_family else candidate.trend_family),
        'vol_percentile': vol_pct,
        'vol_bucket': vol_bucket(vol_pct),
        'session': feature.session if feature else None,
    }
    vwap = fnum(feature.session_vwap_distance_atr) if feature else None
    if vwap is None and feature:
        vwap = fnum(feature.swing_vwap_distance_atr)
    location = {
        'vwap_distance_atr': vwap,
        'value_area_high_distance_atr': fnum(feature.value_area_high_distance_atr) if feature else None,
        'value_area_low_distance_atr': fnum(feature.value_area_low_distance_atr) if feature else None,
        'chandelier_stop_distance_atr': fnum(feature.chandelier_stop_distance_atr) if feature else None,
    }
    return SignalCardOut(
        id=candidate.id,
        action=card_action(candidate),
        status=candidate.status,
        status_reason=candidate.status_reason,
        coin=candidate.coin,
        horizon=candidate.horizon,
        decision_time=candidate.decision_time,
        bar_close=candidate.bar_close,
        source=candidate.source,
        locked_book=candidate.locked_book,
        entry_price=fnum(candidate.entry_price),
        stop_price=fnum(candidate.stop_price),
        pre_options_stop_price=fnum(candidate.pre_options_stop_price),
        upper_barrier_price=fnum(candidate.upper_barrier_price),
        lower_barrier_price=fnum(candidate.lower_barrier_price),
        target_cap_price=fnum(candidate.target_cap_price),
        one_r=fnum(candidate.one_r),
        vertical_deadline=candidate.vertical_deadline,
        edge_hidden=hidden,
        edge_hidden_reason=reason,
        estimates=estimates,
        regime=regime,
        options_line=OptionsLineOut(
            verdict=candidate.options_verdict,
            venue=candidate.options_venue,
            vol_tag=candidate.options_vol_tag,
            dealer_gamma_sign=int(candidate.options_dealer_gamma_sign) if candidate.options_dealer_gamma_sign is not None else None,
            nearest_path_strike=fnum(candidate.options_nearest_path_strike),
            hours_to_expiry=fnum(candidate.options_hours_to_expiry),
            expression=candidate.options_expression,
            size_multiplier=fnum(candidate.size_multiplier),
            influence=fnum(candidate.options_influence),
            confidence=fnum(candidate.options_confidence),
            snapshot_time=candidate.options_snapshot_time,
        ),
        trigger=candidate.trigger,
        setup_family=candidate.setup_family,
        location=location,
        drivers=drivers_from_feature(feature),
        cost_line=cost_line(feature),
        feature_hash=candidate.feature_hash,
        rule_hash=candidate.rule_hash,
        code_version=candidate.code_version,
        on_current_rule_hash=candidate.rule_hash == current_rule_hash(),
    )


def _features_for_candidates(session: Session, candidates: list[Candidate]) -> dict[int, FeatureRow]:
    if not candidates:
        return {}
    ids = [c.feature_row_id for c in candidates]
    rows = session.execute(select(FeatureRow).where(FeatureRow.id.in_(ids))).scalars().all()
    return {row.id: row for row in rows}


def _validate_status(status: str | None) -> None:
    if status is not None and status not in E.CANDIDATE_STATUSES:
        raise HTTPException(status_code=422, detail=f'status must be one of {E.CANDIDATE_STATUSES}')


@router.get('', response_model=CardListOut)
def list_cards(
    session: ReadonlySession,
    source: SourceQuery,
    status: str | None = Query(None),
    coin: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CardListOut:
    _validate_status(status)
    stmt = select(Candidate).where(Candidate.source == source)
    if status:
        stmt = stmt.where(Candidate.status == status)
    if coin:
        stmt = stmt.where(Candidate.coin == coin)
    count_stmt = select(func.count()).select_from(Candidate).where(Candidate.source == source)
    if status:
        count_stmt = count_stmt.where(Candidate.status == status)
    if coin:
        count_stmt = count_stmt.where(Candidate.coin == coin)
    total_n = int(session.scalar(count_stmt) or 0)
    rows = list(session.execute(stmt.order_by(Candidate.decision_time.desc(), Candidate.id.desc()).limit(limit).offset(offset)).scalars().all())
    features = _features_for_candidates(session, rows)
    items = [_serialize_card(c, features.get(c.feature_row_id)) for c in rows]
    return CardListOut(items=items, total=total_n, limit=limit, offset=offset)


@router.get('/{card_id}', response_model=SignalCardOut)
def get_card(session: ReadonlySession, card_id: int, source: SourceQuery) -> SignalCardOut:
    candidate = session.get(Candidate, card_id)
    if candidate is None or candidate.source != source:
        raise HTTPException(status_code=404, detail='card not found')
    feature = session.get(FeatureRow, candidate.feature_row_id)
    return _serialize_card(candidate, feature)
