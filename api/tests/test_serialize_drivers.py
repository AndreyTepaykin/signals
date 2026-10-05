"""Driver field names match FeatureRow columns."""

from __future__ import annotations

from app.models.features import FeatureRow
from app.routers.serialize import DRIVER_FIELDS


def test_driver_fields_are_feature_row_columns():
    columns = set(FeatureRow.__table__.columns.keys())
    for name in DRIVER_FIELDS:
        assert name in columns, f'{name} is not a FeatureRow column'
