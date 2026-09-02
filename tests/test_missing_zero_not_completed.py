from services.analysis_service import AnalysisService


def test_manual_zero_graded_without_submission_is_not_completed():
    submission = {
        "workflow_state": "graded",
        "score": 0,
        "submitted_at": None,
        "attempt": None,
        "missing": True,
    }
    assert AnalysisService._is_completed(submission) is False


def test_manual_zero_graded_without_missing_flag_is_not_completed():
    submission = {
        "workflow_state": "graded",
        "score": 0,
        "submitted_at": None,
        "attempt": None,
    }
    assert AnalysisService._is_completed(submission) is False


def test_real_submitted_zero_is_completed():
    submission = {
        "workflow_state": "graded",
        "score": 0,
        "submitted_at": "2026-08-05T15:00:00Z",
        "attempt": 1,
        "missing": False,
    }
    assert AnalysisService._is_completed(submission) is True


def test_graded_attempt_without_submitted_at_can_be_completed():
    submission = {
        "workflow_state": "graded",
        "score": 80,
        "submitted_at": None,
        "attempt": 1,
        "missing": False,
    }
    assert AnalysisService._is_completed(submission) is True


def test_excused_activity_is_treated_as_completed():
    assert AnalysisService._is_completed({"excused": True, "missing": True}) is True
