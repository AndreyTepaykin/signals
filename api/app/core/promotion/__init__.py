"""The shadow promotion gate (Phase 2 prep): pure, report-only. See `gate` for the contract and `layers` for the pairs."""

from app.core.promotion.gate import (
    PREVIEW_PREFIX,
    VERDICT_INSUFFICIENT,
    VERDICT_PROMOTE,
    VERDICT_REJECT,
    VERDICTS,
    GateCell,
    PairedOutcome,
    evaluate_cell,
    gate_params,
    run_gate,
)
from app.core.promotion.layers import LAYERS, Counterfactual, Layer, get_layer

__all__ = [
    'LAYERS',
    'PREVIEW_PREFIX',
    'VERDICTS',
    'VERDICT_INSUFFICIENT',
    'VERDICT_PROMOTE',
    'VERDICT_REJECT',
    'Counterfactual',
    'GateCell',
    'Layer',
    'PairedOutcome',
    'evaluate_cell',
    'gate_params',
    'get_layer',
    'run_gate',
]
