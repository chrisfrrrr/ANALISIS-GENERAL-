from utils.course_families import course_family_info, group_course_families


def test_groups_canvas_course_shells_by_subject_and_period():
    courses = [
        {"id": 1, "name": "ÁLGEBRA SUPERIOR - SECCIÓN - 10 - 2026 - 1"},
        {"id": 2, "name": "ÁLGEBRA SUPERIOR - SECCIÓN - 11 - 2026 - 1"},
        {"id": 3, "name": "ÁLGEBRA SUPERIOR - SECCIÓN - 12 - 2026 - 1"},
        {"id": 4, "name": "INTRODUCCIÓN A LA ADMINISTRACIÓN II - SECCIÓN - 10 - 2026 - 1"},
    ]
    families = group_course_families(courses)
    assert len(families) == 2
    algebra = next(item for item in families if item["base_name"] == "ÁLGEBRA SUPERIOR")
    assert algebra["name"] == "ÁLGEBRA SUPERIOR · 2026-1"
    assert len(algebra["courses"]) == 3
    assert [c["_family_info"]["section_label"] for c in algebra["courses"]] == [
        "Sección 10", "Sección 11", "Sección 12"
    ]


def test_does_not_mix_different_periods():
    courses = [
        {"id": 1, "name": "MATEMÁTICA - SECCIÓN - 10 - 2026 - 1"},
        {"id": 2, "name": "MATEMÁTICA - SECCIÓN - 10 - 2026 - 2"},
    ]
    families = group_course_families(courses)
    assert len(families) == 2


def test_plain_course_remains_individual_family():
    info = course_family_info({"id": 5, "name": "Matemática General"})
    assert info["base_name"] == "Matemática General"
    assert info["section_label"] is None
    assert info["is_section_shell"] is False
