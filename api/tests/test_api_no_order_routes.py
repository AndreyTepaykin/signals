"""Every HTTP route is read-only GET; no order/trade paths; OpenAPI has no request bodies."""

from __future__ import annotations

from fastapi import FastAPI

from app.main import create_app
from app.routers.route_policy import audit_app_routes


def test_signals_app_passes_route_policy():
    problems = audit_app_routes(create_app())
    assert problems == [], problems


def test_route_policy_flags_bad_throwaway_app():
    bad = FastAPI()

    @bad.post('/orders')
    def _orders():  # noqa: ANN202
        return {}

    @bad.get('/place-order', include_in_schema=False)
    def _place():  # noqa: ANN202
        return {}

    problems = audit_app_routes(bad)
    assert any('orders' in p for p in problems)
    assert any('place-order' in p or 'place' in p for p in problems)
    assert any('POST' in p or 'disallowed methods' in p for p in problems)
