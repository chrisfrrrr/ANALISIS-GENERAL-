from datetime import datetime, timezone

from services.analysis_service import AnalysisService


def test_inactivity_uses_real_canvas_activity_when_available():
    cutoff = datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)
    result = AnalysisService._resolve_inactivity(
        {"last_activity_at": "2026-09-01T23:59:00+00:00"},
        {"start_at": "2026-08-01T00:00:00+00:00"},
        cutoff,
        3,
    )
    assert result["inactivity_estimated"] is False
    assert 23.9 <= result["inactivity_hours"] <= 24.1
    assert result["last_activity_at"] is not None


def test_inactivity_never_accessed_falls_back_to_effective_course_start():
    cutoff = datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)
    result = AnalysisService._resolve_inactivity(
        {
            "last_activity_at": None,
            "created_at": "2026-07-15T00:00:00+00:00",
            "start_at": "2026-08-05T00:00:00+00:00",
        },
        {
            "start_at": "2026-08-01T00:00:00+00:00",
            "term": {"start_at": "2026-07-25T00:00:00+00:00"},
        },
        cutoff,
        3,
    )
    assert result["inactivity_estimated"] is True
    assert result["inactivity_hours"] is not None
    assert result["inactivity_hours"] > 600
    assert "Sin actividad registrada" in result["inactivity_source"]
    assert "Inicio de matrícula" in result["inactivity_source"]


def test_inactivity_without_any_dates_uses_week_based_fallback():
    cutoff = datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)
    result = AnalysisService._resolve_inactivity({}, {}, cutoff, 3)
    assert result["inactivity_estimated"] is True
    assert round(result["inactivity_hours"], 1) == 504.0
