import requests
import pytest

from services.canvas_service import CanvasAPIError, CanvasService


def test_submissions_are_requested_in_small_batches(monkeypatch):
    service = CanvasService("https://example.instructure.com", "token")
    calls = []
    progress = []

    def fake_get_paginated(path, params=None, max_pages=100):
        calls.append((path, list(params or []), max_pages))
        return []

    monkeypatch.setattr(service, "get_paginated", fake_get_paginated)
    service.list_submissions(
        99,
        student_ids=[str(value) for value in range(60)],
        assignment_ids=[str(value) for value in range(41)],
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert len(calls) == 6  # 3 lotes de estudiantes x 2 lotes de actividades
    assert progress[-1] == (6, 6)
    for _, params, _ in calls:
        student_values = [value for key, value in params if key == "student_ids[]"]
        assignment_values = [value for key, value in params if key == "assignment_ids[]"]
        include_values = [value for key, value in params if key == "include[]"]
        assert len(student_values) <= 25
        assert len(assignment_values) <= 40
        assert include_values == []


def test_timeout_message_is_friendly(monkeypatch):
    service = CanvasService("https://example.instructure.com", "token")

    def raise_timeout(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("technical pool detail")

    monkeypatch.setattr(service.session, "request", raise_timeout)
    with pytest.raises(CanvasAPIError) as error:
        service.get("/api/v1/users/self/profile")

    message = str(error.value)
    assert "tardó demasiado" in message
    assert "technical pool detail" not in message


def test_course_student_directory_requests_identity_fields(monkeypatch):
    service = CanvasService("https://example.instructure.com", "token")
    captured = {}

    def fake_get_paginated(path, params=None, max_pages=100):
        captured["path"] = path
        captured["params"] = list(params or [])
        return []

    monkeypatch.setattr(service, "get_paginated", fake_get_paginated)
    service.list_course_students(77)

    assert captured["path"] == "/api/v1/courses/77/users"
    include_values = [value for key, value in captured["params"] if key == "include[]"]
    assert "email" in include_values
    assert "enrollments" in include_values


def _response(status_code, payload=None, headers=None):
    import json

    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    response._content = json.dumps(payload if payload is not None else {}).encode("utf-8")
    response.url = "https://example.instructure.com/api/v1/test"
    return response


def test_rate_limit_429_is_retried_automatically(monkeypatch):
    service = CanvasService("https://example.instructure.com", "rate-token", rate_limit_retries=2)
    responses = [
        _response(429, {"message": "rate limited"}, {"Retry-After": "0", "X-Rate-Limit-Remaining": "0"}),
        _response(200, {"ok": True}, {"X-Rate-Limit-Remaining": "500"}),
    ]
    calls = []

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(service.session, "request", fake_request)
    payload = service.get("/api/v1/test")

    assert payload == {"ok": True}
    assert len(calls) == 2


def test_page_views_stops_after_first_rate_limit(monkeypatch):
    from datetime import datetime, timezone

    service = CanvasService("https://example.instructure.com", "page-view-token")
    calls = []

    def fake_page_views(user_id, *args, **kwargs):
        calls.append(str(user_id))
        raise CanvasAPIError("Canvas limitado", status_code=429)

    monkeypatch.setattr(service, "list_page_views", fake_page_views)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 2, tzinfo=timezone.utc)

    sessions, errors = service.fetch_page_view_sessions(["1", "2", "3"], start, end, 77)

    assert calls == ["1"]
    assert sessions == {"1": None, "2": None, "3": None}
    assert set(errors) == {"1", "2", "3"}
