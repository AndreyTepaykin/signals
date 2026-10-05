"""M9 API integration tests (throwaway schema, rolled back)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core import constants as C
from app.core.hashing import rule_hash
from app.core.stats import params as P
from app.main import create_app
from app.routers.deps import get_readonly_session
from app.routers.stats_cache import clear_stats_cache
from conftest import BAR_CLOSE, make_candidate, make_feature_row
from tests.test_stats_job import add_label, cand

CURRENT = rule_hash()


@pytest.fixture
def api_client(session, db_schema, db_engine):
    clear_stats_cache()

    def override():
        yield session

    app = create_app()
    app.dependency_overrides[get_readonly_session] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    clear_stats_cache()


def test_cards_edge_hidden_when_pool_small(api_client, session):
    c = make_candidate(
        session,
        rule_hash=CURRENT,
        pool_n=5,
        pool_level='full_slice',
        expected_r=Decimal('0.1'),
        p_upper_before_stop=Decimal('0.55'),
        feature_overrides={'realized_vol_pct': Decimal('0.4'), 'session': 'us', 'trend_family': 'trend'},
    )
    r = api_client.get('/cards?source=live')
    assert r.status_code == 200
    body = r.json()
    assert body['total'] >= 1
    card = next(x for x in body['items'] if x['id'] == c.id)
    assert card['edge_hidden'] is True
    assert card['estimates']['expected_r'] is None
    assert 'probability' not in card['options_line']


def test_tracker_lists_labeled_row(api_client, session):
    c = cand(session, 0)
    add_label(session, c)
    r = api_client.get('/tracker?source=live')
    assert r.status_code == 200
    data = r.json()
    assert data['total'] == 1
    assert data['items'][0]['candidate_id'] == c.id


def test_calibration_bins_never_below_min(api_client, session):
    for i in range(25):
        c = cand(session, i * 5, source='replay')
        add_label(session, c)
    clear_stats_cache()
    r = api_client.get('/stats/calibration?source=replay')
    assert r.status_code == 200
    payload = r.json()
    for cohort in payload['cohorts']:
        for key in ('reliability', 'reliability_all_statuses'):
            for b in cohort[key]['bins']:
                assert b['n_independent'] >= P.MIN_BIN_OUTCOMES


def test_websocket_hello(api_client):
    with api_client.websocket_connect('/ws') as ws:
        msg = ws.receive_json()
        assert msg['type'] == 'hello'
        assert 'fingerprint' in msg
        assert 'stand_down' in msg


def test_stand_down_endpoint(api_client):
    r = api_client.get('/stand-down')
    assert r.status_code == 200
    assert 'active' in r.json()
