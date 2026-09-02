import io
import zipfile

import pandas as pd

from services.combined_referral_service import combine_course_analyses, generate_combined_referral_package


def _row(user, carne, course_id, course_name, pending, inactivity, risk, advisor="Astrid"):
    return {
        "canvas_user_id": str(user),
        "carne": str(carne),
        "student_name": f"Estudiante {carne}",
        "email": f"{carne}@example.edu",
        "career": "Prueba",
        "course_id": str(course_id),
        "course_name": course_name,
        "section_name": "Sección A",
        "week_number": 3,
        "overall_risk": risk,
        "intervention_priority": "Prioritaria" if risk == "Alto" else "Seguimiento",
        "expected_activities": 6,
        "completed_activities": 6 - pending,
        "pending_count": pending,
        "inactivity_hours": inactivity,
        "average_grade": 70,
        "completion_percentage": (6 - pending) / 6 * 100,
        "pending_assignments": [f"Pendiente {i+1}" for i in range(pending)],
        "last_activity_at": "2026-09-01T00:00:00+00:00",
        "analysis_cutoff": "2026-09-02T23:59:59+00:00",
        "reasons": ["Prueba"],
        "asesor_bienestar": advisor,
    }


def test_combines_two_courses_and_sums_pending_and_inactivity():
    df = pd.DataFrame([
        _row(1, 1001, 10, "Álgebra", 2, 50, "Moderado"),
        _row(1, 1001, 20, "Aritmética", 3, 80, "Alto"),
    ])
    combined = combine_course_analyses(df, [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Aritmética"}])
    assert len(combined) == 1
    row = combined.iloc[0]
    assert row["course_1_pending"] == 2
    assert row["course_2_pending"] == 3
    assert row["pending_total"] == 5
    assert row["inactivity_total_hours"] == 130
    assert row["overall_risk"] == "Alto"
    assert row["asesor_bienestar"] == "Astrid"


def test_student_present_in_only_one_course_is_preserved():
    df = pd.DataFrame([_row(2, 1002, 10, "Álgebra", 1, 24, "Moderado")])
    combined = combine_course_analyses(df, [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Aritmética"}])
    assert len(combined) == 1
    row = combined.iloc[0]
    assert row["courses_present"] == 1
    assert row["course_2_section"] == "No aparece en el curso"
    assert row["pending_total"] == 1


def test_package_contains_one_individual_referral_per_student():
    df = pd.DataFrame([
        _row(1, 1001, 10, "Álgebra", 2, 50, "Moderado"),
        _row(1, 1001, 20, "Aritmética", 3, 80, "Alto"),
    ])
    combined = combine_course_analyses(df, [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Aritmética"}])
    package, records = generate_combined_referral_package(combined, academic_advisor="Asesor prueba")
    assert len(records) == 1
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        names = zf.namelist()
    assert any(name.endswith("Informe_unificado.xlsx") for name in names)
    assert sum("Derivacion_Unificada_" in name for name in names) == 1


def test_present_student_never_leaves_course_inactivity_blank():
    c1 = _row(7, 7007, 10, "Álgebra", 2, 30, "Moderado")
    c2 = _row(7, 7007, 20, "Administración", 6, None, "Alto")
    c2["last_activity_at"] = None
    c2["inactivity_reference_at"] = None
    c2["inactivity_source"] = None
    c2["week_number"] = 3

    combined = combine_course_analyses(
        pd.DataFrame([c1, c2]),
        [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Administración"}],
    )

    row = combined.iloc[0]
    assert row["courses_present"] == 2
    assert row["course_2_inactivity_hours"] == 504.0
    assert row["course_2_inactivity_estimated"] is True or bool(row["course_2_inactivity_estimated"]) is True
    assert "Respaldo final" in row["course_2_inactivity_source"]
    assert row["inactivity_total_hours"] == 534.0


def test_consolidation_recovers_hours_from_last_activity_when_numeric_value_is_missing():
    c1 = _row(8, 8008, 10, "Álgebra", 1, 20, "Moderado")
    c2 = _row(8, 8008, 20, "Administración", 2, None, "Moderado")
    c2["last_activity_at"] = "2026-09-01T23:59:59+00:00"

    combined = combine_course_analyses(
        pd.DataFrame([c1, c2]),
        [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Administración"}],
    )

    row = combined.iloc[0]
    assert 23.9 <= row["course_2_inactivity_hours"] <= 24.1
    assert row["course_2_inactivity_estimated"] is False or bool(row["course_2_inactivity_estimated"]) is False
