import math

import pandas as pd

from services.database_service import _coerce_integer, _normalize_snapshot_record


def test_coerce_integer_accepts_decimal_strings_and_floats():
    assert _coerce_integer("0.0") == 0
    assert _coerce_integer("12.0") == 12
    assert _coerce_integer(7.0) == 7
    assert _coerce_integer(3) == 3


def test_coerce_integer_handles_missing_values():
    assert _coerce_integer(None) == 0
    assert _coerce_integer(float("nan")) == 0
    assert _coerce_integer("nan") == 0
    assert _coerce_integer(pd.NA, default=None) is None


def test_snapshot_record_normalizes_postgres_integer_fields_only():
    record = {
        "week_number": "19.0",
        "total_weeks": 21.0,
        "total_activities": "24.0",
        "expected_activities": "24.0",
        "completed_activities": "13.0",
        "completed_expected": 13.0,
        "pending_count": "11.0",
        "late_count": "0.0",
        "early_count": 2.0,
        "weekly_sessions": "0.0",
        "average_grade": 87.5,
        "completion_percentage": 54.17,
        "inactivity_hours": 93.25,
    }

    normalized = _normalize_snapshot_record(record)

    for field in [
        "week_number", "total_weeks", "total_activities", "expected_activities",
        "completed_activities", "completed_expected", "pending_count", "late_count",
        "early_count", "weekly_sessions",
    ]:
        assert isinstance(normalized[field], int)

    assert normalized["week_number"] == 19
    assert normalized["late_count"] == 0
    assert normalized["weekly_sessions"] == 0
    assert normalized["average_grade"] == 87.5
    assert normalized["completion_percentage"] == 54.17
    assert normalized["inactivity_hours"] == 93.25
