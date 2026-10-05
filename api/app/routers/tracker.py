"""Outcome tracker endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.decisions import Candidate
from app.models.labels import Label
from app.models.options import OptionsMetric
from app.routers.deps import ReadonlySession, SourceQuery
from app.routers.serialize import fnum

router = APIRouter(prefix='/tracker', tags=['tracker'])


class TrackerOptionsSnapshot(BaseModel):
    verdict: str | None
    venue: str | None
    dealer_gamma_sign: int | None
    nearest_path_strike: float | None
    hours_to_expiry: float | None
    expression: str | None
    gamma_flip_distance_atr: float | None
    nearest_path_strike_distance_atr: float | None
    skew_25d: float | None
    iv_minus_rv: float | None


class TrackerRowOut(BaseModel):
    candidate_id: int
    decision_time: datetime
    fill_time: datetime
    resolved_at: datetime
    feature_hash: str
    rule_hash: str
    code_version: str
    first_barrier: str
    stored_r: float | None
    price_r: float | None
    fees_r: float | None
    funding_r: float | None
    mae_r: float | None
    mfe_r: float | None
    hours_to_mfe: float | None
    hours_to_resolution: float | None
    stop_price: float | None
    upper_barrier_price: float | None
    target_cap_price: float | None
    barrier_ambiguous: bool
    gapped_stop: bool
    funding_partial: bool
    no_entry: bool
    options_snapshot: TrackerOptionsSnapshot | None
    regime_trend_family: str | None
    regime_vol_pct: float | None
    regime_session: str | None
    regime_event_flag: bool | None
    regime_is_weekend: bool | None
    cmi_btc_return: float | None
    cmi_eth_btc_return: float | None
    cmi_dxy_return: float | None
    cmi_nasdaq_return: float | None
    cmi_alt_residual_return: float | None
    lead_venue: str | None
    lead_venue_oi_rose: bool | None
    spread_bps: float | None
    cost_assumption_bps: float
    expiry_during_hold: bool | None
    side_exit_partials_r: float | None
    side_exit_move_to_flat_r: float | None
    side_exit_time_stop_r: float | None
    coin: str
    horizon: str
    direction: str
    status: str


class TrackerListOut(BaseModel):
    items: list[TrackerRowOut]
    total: int
    limit: int
    offset: int


def _options_snapshot(metric: OptionsMetric | None, candidate: Candidate) -> TrackerOptionsSnapshot | None:
    if candidate.options_verdict is None and metric is None:
        return None
    return TrackerOptionsSnapshot(
        verdict=candidate.options_verdict,
        venue=candidate.options_venue,
        dealer_gamma_sign=int(candidate.options_dealer_gamma_sign) if candidate.options_dealer_gamma_sign is not None else None,
        nearest_path_strike=fnum(candidate.options_nearest_path_strike),
        hours_to_expiry=fnum(candidate.options_hours_to_expiry),
        expression=candidate.options_expression,
        gamma_flip_distance_atr=fnum(metric.gamma_flip_distance_atr) if metric else None,
        nearest_path_strike_distance_atr=fnum(metric.nearest_path_strike_distance_atr) if metric else None,
        skew_25d=fnum(metric.skew_25d) if metric else None,
        iv_minus_rv=fnum(metric.iv_minus_rv) if metric else None,
    )


def _row(candidate: Candidate, label: Label, metric: OptionsMetric | None) -> TrackerRowOut:
    from app.models import enums as E

    return TrackerRowOut(
        candidate_id=candidate.id,
        decision_time=candidate.decision_time,
        fill_time=label.fill_time,
        resolved_at=label.resolved_at,
        feature_hash=label.feature_hash,
        rule_hash=label.rule_hash,
        code_version=label.code_version,
        first_barrier=label.first_barrier,
        stored_r=fnum(label.stored_r),
        price_r=fnum(label.price_r),
        fees_r=fnum(label.fees_r),
        funding_r=fnum(label.funding_r),
        mae_r=fnum(label.mae_r),
        mfe_r=fnum(label.mfe_r),
        hours_to_mfe=fnum(label.hours_to_mfe),
        hours_to_resolution=fnum(label.hours_to_resolution),
        stop_price=fnum(candidate.stop_price),
        upper_barrier_price=fnum(candidate.upper_barrier_price),
        target_cap_price=fnum(candidate.target_cap_price),
        barrier_ambiguous=label.barrier_ambiguous,
        gapped_stop=label.gapped_stop,
        funding_partial=label.funding_partial,
        no_entry=label.first_barrier == E.BARRIER_NO_ENTRY,
        options_snapshot=_options_snapshot(metric, candidate),
        regime_trend_family=label.regime_trend_family,
        regime_vol_pct=fnum(label.regime_vol_pct),
        regime_session=label.regime_session,
        regime_event_flag=label.regime_event_flag,
        regime_is_weekend=label.regime_is_weekend,
        cmi_btc_return=fnum(label.cmi_btc_return),
        cmi_eth_btc_return=fnum(label.cmi_eth_btc_return),
        cmi_dxy_return=fnum(label.cmi_dxy_return),
        cmi_nasdaq_return=fnum(label.cmi_nasdaq_return),
        cmi_alt_residual_return=fnum(label.cmi_alt_residual_return),
        lead_venue=label.lead_venue,
        lead_venue_oi_rose=label.lead_venue_oi_rose,
        spread_bps=fnum(label.spread_bps),
        cost_assumption_bps=float(label.cost_assumption_bps),
        expiry_during_hold=label.expiry_during_hold,
        side_exit_partials_r=fnum(label.side_exit_partials_r),
        side_exit_move_to_flat_r=fnum(label.side_exit_move_to_flat_r),
        side_exit_time_stop_r=fnum(label.side_exit_time_stop_r),
        coin=candidate.coin,
        horizon=candidate.horizon,
        direction=candidate.direction,
        status=candidate.status,
    )


@router.get('', response_model=TrackerListOut)
def list_tracker(
    session: ReadonlySession,
    source: SourceQuery,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> TrackerListOut:
    count_stmt = (
        select(func.count())
        .select_from(Label)
        .join(Candidate, Candidate.id == Label.candidate_id)
        .where(Candidate.source == source)
    )
    total = int(session.scalar(count_stmt) or 0)
    stmt = (
        select(Candidate, Label, OptionsMetric)
        .join(Label, Label.candidate_id == Candidate.id)
        .outerjoin(OptionsMetric, OptionsMetric.id == Candidate.options_metric_id)
        .where(Candidate.source == source)
        .order_by(Candidate.decision_time.desc(), Candidate.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [_row(c, l, m) for c, l, m in session.execute(stmt).all()]
    return TrackerListOut(items=items, total=total, limit=limit, offset=offset)
