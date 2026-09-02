from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from typing import Any

_DASH_SPLIT = re.compile(r"\s*[-–—]\s*")
_SPACE = re.compile(r"\s+")


def _plain(value: Any) -> str:
    text = str(value or "").strip()
    return _SPACE.sub(" ", text)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _plain(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def course_family_info(course: dict[str, Any]) -> dict[str, Any]:
    """Interpreta cursos Canvas creados como un shell por sección.

    Ejemplo:
        ÁLGEBRA SUPERIOR - SECCIÓN - 10 - 2026 - 1
    se convierte en la familia:
        ÁLGEBRA SUPERIOR · 2026-1
    y etiqueta de sección:
        Sección 10

    Si el nombre no sigue ese patrón, el curso se conserva como una familia
    individual y después la UI puede consultar sus secciones internas reales.
    """
    name = _plain(course.get("name") or course.get("course_code") or f"Curso {course.get('id', '')}")
    tokens = [token.strip() for token in _DASH_SPLIT.split(name) if token.strip()]

    section_index = None
    for index, token in enumerate(tokens):
        if _fold(token) in {"seccion", "section"}:
            section_index = index
            break

    base_name = name
    section_label = None
    year = None
    term = None
    is_section_shell = False

    if section_index is not None and section_index + 1 < len(tokens):
        base_tokens = tokens[:section_index]
        if base_tokens:
            base_name = " - ".join(base_tokens)
            raw_section = tokens[section_index + 1]
            section_label = f"Sección {raw_section}"
            is_section_shell = True

            tail = tokens[section_index + 2 :]
            if tail and re.fullmatch(r"20\d{2}", tail[0]):
                year = tail[0]
                if len(tail) > 1 and re.fullmatch(r"\d{1,2}", tail[1]):
                    term = tail[1]

    # Segundo formato frecuente: "Materia SECCIÓN 10 2026-1".
    if not is_section_shell:
        match = re.match(
            r"^(?P<base>.+?)\s+SECCI[ÓO]N\s*[:#-]?\s*(?P<section>[A-Za-z0-9]+)"
            r"(?:\s+|\s*[-–—]\s*)(?P<year>20\d{2})\s*[-–—/]\s*(?P<term>\d{1,2})\s*$",
            name,
            flags=re.IGNORECASE,
        )
        if match:
            base_name = _plain(match.group("base"))
            section_label = f"Sección {match.group('section')}"
            year = match.group("year")
            term = match.group("term")
            is_section_shell = True

    period = f"{year}-{term}" if year and term else (year or "")
    family_key = _fold(base_name)
    if period:
        family_key = f"{family_key}|{period}"

    display_name = base_name
    if period:
        display_name = f"{base_name} · {period}"

    return {
        "family_key": family_key,
        "base_name": base_name,
        "display_name": display_name,
        "period": period,
        "section_label": section_label,
        "is_section_shell": is_section_shell,
    }


def group_course_families(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa shells de Canvas que representan secciones de la misma materia."""
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for course in courses:
        if not isinstance(course, dict) or course.get("id") is None:
            continue
        info = course_family_info(course)
        key = info["family_key"]
        if key not in groups:
            groups[key] = {
                "id": f"family:{key}",
                "key": key,
                "name": info["display_name"],
                "base_name": info["base_name"],
                "period": info["period"],
                "courses": [],
                "section_shells": False,
            }
        enriched = dict(course)
        enriched["_family_info"] = info
        groups[key]["courses"].append(enriched)
        groups[key]["section_shells"] = groups[key]["section_shells"] or bool(info["is_section_shell"])

    result = list(groups.values())
    for group in result:
        group["courses"] = sorted(
            group["courses"],
            key=lambda item: (
                _natural_section_key((item.get("_family_info") or {}).get("section_label")),
                str(item.get("id")),
            ),
        )
    return result


def _natural_section_key(label: Any) -> tuple[int, Any]:
    text = _plain(label)
    match = re.search(r"(\d+)", text)
    if match:
        return (0, int(match.group(1)))
    return (1, text.casefold())


def family_option_label(family: dict[str, Any]) -> str:
    count = len(family.get("courses") or [])
    suffix = "sección" if count == 1 else "secciones"
    return f"{family.get('name') or 'Curso'} · {count} {suffix} Canvas"


def shell_option_label(course: dict[str, Any]) -> str:
    info = course.get("_family_info") or course_family_info(course)
    section = info.get("section_label") or course.get("name") or course.get("course_code") or "Curso completo"
    students = course.get("total_students")
    students_text = "—" if students in (None, "") else str(students)
    return f"{section} · {students_text} estudiantes · ID {course.get('id')}"
