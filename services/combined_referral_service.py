from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from utils.dates import hours_between, parse_datetime
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

NAVY = "0F1C75"
BLUE = "1C73F5"
GREEN = "00AB0D"
AMBER = "FFB500"
RED = "C62828"
LIGHT_BLUE = "EAF2FF"
LIGHT_RED = "FDECEC"
LIGHT_AMBER = "FFF6D9"
LIGHT_GRAY = "F3F5F8"
WHITE = "FFFFFF"
DARK = "172033"

THIN_GRAY = Side(style="thin", color="D7DCE5")
BORDER = Border(left=THIN_GRAY, right=THIN_GRAY, top=THIN_GRAY, bottom=THIN_GRAY)

RISK_RANK = {"Sin datos": -1, "Bajo": 0, "Moderado": 1, "Alto": 2}
PRIORITY_RANK = {"Sin clasificar": -1, "Preventiva": 0, "Seguimiento": 1, "Prioritaria": 2, "Urgente": 3}


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if _missing(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_filename(value: Any, max_length: int = 80) -> str:
    text = str(value or "sin_dato").strip()
    text = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúÑñ0-9._-]+", "_", text)
    return text.strip("_")[:max_length] or "sin_dato"


def _student_key(row: dict[str, Any]) -> str:
    user_id = str(row.get("canvas_user_id") or "").strip()
    if user_id:
        return f"canvas:{user_id}"
    carne = str(row.get("carne") or "").strip()
    if carne:
        return f"carne:{carne}"
    email = str(row.get("email") or "").strip().lower()
    return f"email:{email}"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if _missing(value):
        return []
    text = str(value).strip()
    return [text] if text else []


def _max_label(values: list[Any], ranking: dict[str, int], default: str) -> str:
    labels = [str(value) for value in values if str(value) in ranking]
    if not labels:
        return default
    return max(labels, key=lambda label: ranking[label])


def _resolve_consolidated_inactivity(row: dict[str, Any]) -> dict[str, Any]:
    """Garantía final para que un estudiante presente nunca salga sin desconexión.

    El análisis normal ya resuelve ``last_activity_at`` nulo. Esta segunda capa
    protege la consolidación cuando una fila parcial, heredada o duplicada llega
    con ``inactivity_hours`` vacío. Se intenta reconstruir el valor sin nuevas
    llamadas a Canvas y se marca explícitamente como estimado.
    """
    raw_hours = row.get("inactivity_hours")
    if not _missing(raw_hours):
        try:
            return {
                "inactivity_hours": round(float(raw_hours), 1),
                "last_activity_at": row.get("last_activity_at"),
                "inactivity_reference_at": row.get("inactivity_reference_at"),
                "inactivity_estimated": bool(row.get("inactivity_estimated") or False),
                "inactivity_source": row.get("inactivity_source") or "Última actividad reportada por Canvas",
            }
        except (TypeError, ValueError):
            pass

    cutoff = parse_datetime(row.get("analysis_cutoff"))
    if cutoff is None:
        cutoff = datetime.now(timezone.utc)

    # Primero se reconstruye desde cualquier referencia ya disponible.
    for label, raw_reference, estimated in (
        ("Última actividad reportada por Canvas", row.get("last_activity_at"), False),
        (row.get("inactivity_source") or "Referencia previa del análisis", row.get("inactivity_reference_at"), True),
    ):
        reference = parse_datetime(raw_reference)
        if reference is not None:
            recovered = hours_between(reference, cutoff)
            if recovered is not None:
                return {
                    "inactivity_hours": round(recovered, 1),
                    "last_activity_at": row.get("last_activity_at") if not estimated else None,
                    "inactivity_reference_at": reference.isoformat(),
                    "inactivity_estimated": estimated,
                    "inactivity_source": str(label),
                }

    # Último respaldo: si el estudiante está presente en el curso y no existe
    # ninguna fecha utilizable, la semana seleccionada ofrece un mínimo
    # consistente con el mismo criterio utilizado por AnalysisService.
    try:
        safe_week = max(1, int(float(row.get("week_number") or 1)))
    except (TypeError, ValueError):
        safe_week = 1
    reference = cutoff - timedelta(days=7 * safe_week)
    recovered = hours_between(reference, cutoff)
    return {
        "inactivity_hours": round(float(recovered or 0.0), 1),
        "last_activity_at": None,
        "inactivity_reference_at": reference.isoformat(),
        "inactivity_estimated": True,
        "inactivity_source": "Sin actividad registrada · Respaldo final según semana analizada",
    }


def _course_payload(row: dict[str, Any] | None, course: dict[str, Any]) -> dict[str, Any]:
    if row is None:
        return {
            "present": False,
            "course_id": str(course.get("id") or ""),
            "course_name": course.get("name") or course.get("course_code") or "Curso",
            "section_name": "No aparece en el curso",
            "week_number": None,
            "overall_risk": "Sin datos",
            "intervention_priority": "Sin clasificar",
            "expected_activities": 0,
            "completed_activities": 0,
            "pending_count": 0,
            "inactivity_hours": None,
            "average_grade": None,
            "completion_percentage": 0.0,
            "pending_assignments": [],
            "last_activity_at": None,
            "inactivity_reference_at": None,
            "inactivity_estimated": False,
            "inactivity_source": "Sin datos",
            "reasons": [],
        }
    inactivity = _resolve_consolidated_inactivity(row)
    return {
        "present": True,
        "course_id": str(row.get("course_id") or course.get("id") or ""),
        "course_name": row.get("course_name") or course.get("name") or "Curso",
        "section_name": row.get("section_name") or "Sin sección",
        "week_number": row.get("week_number"),
        "overall_risk": row.get("overall_risk") or "Sin datos",
        "intervention_priority": row.get("intervention_priority") or "Sin clasificar",
        "expected_activities": int(_number(row.get("expected_activities"))),
        "completed_activities": int(_number(row.get("completed_activities"))),
        "pending_count": int(_number(row.get("pending_count"))),
        "inactivity_hours": inactivity["inactivity_hours"],
        "average_grade": None if _missing(row.get("average_grade")) else round(_number(row.get("average_grade")), 2),
        "completion_percentage": round(_number(row.get("completion_percentage")), 2),
        "pending_assignments": _as_list(row.get("pending_assignments")),
        "last_activity_at": inactivity["last_activity_at"],
        "inactivity_reference_at": inactivity["inactivity_reference_at"],
        "inactivity_estimated": inactivity["inactivity_estimated"],
        "inactivity_source": inactivity["inactivity_source"],
        "reasons": _as_list(row.get("reasons")),
    }


def combine_course_analyses(
    analyses: pd.DataFrame,
    courses: list[dict[str, Any]],
) -> pd.DataFrame:
    """Convierte filas estudiante-curso en una sola fila por estudiante para dos cursos."""
    if analyses is None or analyses.empty:
        return pd.DataFrame()
    if len(courses) != 2:
        raise ValueError("La consolidación requiere exactamente dos cursos.")

    work = analyses.copy()
    work["_student_key"] = [
        _student_key(row) for row in work.to_dict(orient="records")
    ]
    work["_risk_rank"] = work.get("overall_risk", pd.Series(index=work.index, dtype=str)).map(RISK_RANK).fillna(-1)
    work = work.sort_values(["_student_key", "course_id", "_risk_rank"], ascending=[True, True, False])
    work = work.drop_duplicates(subset=["_student_key", "course_id"], keep="first")

    rows: list[dict[str, Any]] = []
    course_ids = [str(course.get("id") or "") for course in courses]

    for student_key, group in work.groupby("_student_key", sort=False):
        group_records = group.to_dict(orient="records")
        by_course = {str(item.get("course_id") or ""): item for item in group_records}
        c1 = _course_payload(by_course.get(course_ids[0]), courses[0])
        c2 = _course_payload(by_course.get(course_ids[1]), courses[1])

        identity_source = group_records[0]
        advisors = []
        for item in group_records:
            advisor = str(item.get("asesor_bienestar") or item.get("advisor_name") or "").strip()
            if advisor and advisor != "Sin asignar" and advisor not in advisors:
                advisors.append(advisor)
        advisor = advisors[0] if advisors else "Sin asignar"

        expected_total = c1["expected_activities"] + c2["expected_activities"]
        completed_total = c1["completed_activities"] + c2["completed_activities"]
        pending_total = c1["pending_count"] + c2["pending_count"]
        inactivity_values = [value for value in [c1["inactivity_hours"], c2["inactivity_hours"]] if value is not None]
        inactivity_total = round(sum(inactivity_values), 1) if inactivity_values else None
        grades = [value for value in [c1["average_grade"], c2["average_grade"]] if value is not None]
        average_combined = round(sum(grades) / len(grades), 2) if grades else None
        combined_completion = round(min(100.0, completed_total / expected_total * 100.0), 2) if expected_total else 0.0

        risk = _max_label([c1["overall_risk"], c2["overall_risk"]], RISK_RANK, "Sin datos")
        priority = _max_label(
            [c1["intervention_priority"], c2["intervention_priority"]],
            PRIORITY_RANK,
            "Sin clasificar",
        )

        rows.append(
            {
                "student_key": student_key,
                "canvas_user_id": str(identity_source.get("canvas_user_id") or ""),
                "carne": str(identity_source.get("carne") or ""),
                "student_name": identity_source.get("student_name") or "Estudiante",
                "email": identity_source.get("email") or "",
                "career": identity_source.get("career") or "",
                "asesor_bienestar": advisor,
                "advisor_conflict": len(advisors) > 1,
                "advisor_candidates": " / ".join(advisors),
                "courses_present": int(c1["present"]) + int(c2["present"]),
                "overall_risk": risk,
                "intervention_priority": priority,
                "expected_total": expected_total,
                "completed_total": completed_total,
                "pending_total": pending_total,
                "inactivity_total_hours": inactivity_total,
                "average_combined": average_combined,
                "completion_combined": combined_completion,
                "course_1_id": c1["course_id"],
                "course_1_name": c1["course_name"],
                "course_1_section": c1["section_name"],
                "course_1_week": c1["week_number"],
                "course_1_risk": c1["overall_risk"],
                "course_1_priority": c1["intervention_priority"],
                "course_1_expected": c1["expected_activities"],
                "course_1_completed": c1["completed_activities"],
                "course_1_pending": c1["pending_count"],
                "course_1_inactivity_hours": c1["inactivity_hours"],
                "course_1_average": c1["average_grade"],
                "course_1_completion": c1["completion_percentage"],
                "course_1_pending_assignments": c1["pending_assignments"],
                "course_1_last_activity_at": c1["last_activity_at"],
                "course_1_inactivity_reference_at": c1["inactivity_reference_at"],
                "course_1_inactivity_estimated": c1["inactivity_estimated"],
                "course_1_inactivity_source": c1["inactivity_source"],
                "course_1_reasons": c1["reasons"],
                "course_2_id": c2["course_id"],
                "course_2_name": c2["course_name"],
                "course_2_section": c2["section_name"],
                "course_2_week": c2["week_number"],
                "course_2_risk": c2["overall_risk"],
                "course_2_priority": c2["intervention_priority"],
                "course_2_expected": c2["expected_activities"],
                "course_2_completed": c2["completed_activities"],
                "course_2_pending": c2["pending_count"],
                "course_2_inactivity_hours": c2["inactivity_hours"],
                "course_2_average": c2["average_grade"],
                "course_2_completion": c2["completion_percentage"],
                "course_2_pending_assignments": c2["pending_assignments"],
                "course_2_last_activity_at": c2["last_activity_at"],
                "course_2_inactivity_reference_at": c2["inactivity_reference_at"],
                "course_2_inactivity_estimated": c2["inactivity_estimated"],
                "course_2_inactivity_source": c2["inactivity_source"],
                "course_2_reasons": c2["reasons"],
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["_risk_rank"] = result["overall_risk"].map(RISK_RANK).fillna(-1)
    return result.sort_values(
        ["_risk_rank", "pending_total", "student_name"],
        ascending=[False, False, True],
    ).drop(columns="_risk_rank").reset_index(drop=True)


def build_combined_reason(row: dict[str, Any]) -> str:
    parts = [
        f"El estudiante presenta {int(_number(row.get('pending_total')))} actividad(es) pendiente(s) acumuladas entre los dos cursos."
    ]
    for idx in (1, 2):
        course = row.get(f"course_{idx}_name") or f"Curso {idx}"
        section = row.get(f"course_{idx}_section") or "Sin sección"
        pending = int(_number(row.get(f"course_{idx}_pending")))
        inactivity = row.get(f"course_{idx}_inactivity_hours")
        risk = row.get(f"course_{idx}_risk") or "Sin datos"
        if section == "No aparece en el curso":
            parts.append(f"{course}: el estudiante no aparece dentro de las secciones seleccionadas.")
            continue
        text = f"{course} ({section}): {pending} pendiente(s), riesgo {risk}"
        if not _missing(inactivity):
            qualifier = " estimada" if bool(row.get(f"course_{idx}_inactivity_estimated")) else ""
            text += f" e inactividad{qualifier} de {float(inactivity):.1f} horas"
        parts.append(text + ".")
    if not _missing(row.get("inactivity_total_hours")):
        parts.append(
            f"La suma referencial de horas de desconexión reportadas por curso es {float(row['inactivity_total_hours']):.1f} horas."
        )
    if row.get("advisor_conflict"):
        parts.append(f"Se detectaron asignaciones distintas de bienestar: {row.get('advisor_candidates')}. Requiere validación.")
    return " ".join(parts)


def _style_title(ws, title: str, subtitle: str, end_col: int) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=end_col)
    ws.cell(1, 1, title)
    ws.cell(1, 1).font = Font(size=16, bold=True, color=WHITE)
    ws.cell(1, 1).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(1, 1).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=end_col)
    ws.cell(2, 1, subtitle)
    ws.cell(2, 1).fill = PatternFill("solid", fgColor=LIGHT_BLUE)
    ws.cell(2, 1).alignment = Alignment(horizontal="center")


def _label_value(ws, row: int, label: str, value: Any, end_col: int = 6) -> None:
    ws.cell(row, 1, label)
    ws.cell(row, 1).font = Font(bold=True, color=NAVY)
    ws.cell(row, 1).fill = PatternFill("solid", fgColor=LIGHT_GRAY)
    ws.cell(row, 1).border = BORDER
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=end_col)
    ws.cell(row, 2, "" if _missing(value) else str(value))
    ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
    for col in range(2, end_col + 1):
        ws.cell(row, col).border = BORDER


def _add_course_detail_sheet(wb: Workbook, row: dict[str, Any]) -> None:
    ws = wb.create_sheet("Detalle por curso")
    headers = [
        "Curso", "Sección", "Semana", "Riesgo", "Prioridad", "Esperadas", "Completadas",
        "Pendientes", "Inactividad (h)", "Promedio (%)", "Avance (%)", "Última actividad",
        "Origen de desconexión", "Referencia del cálculo",
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(1, col, header)
        cell.font = Font(bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for idx in (1, 2):
        values = [
            row.get(f"course_{idx}_name"), row.get(f"course_{idx}_section"), row.get(f"course_{idx}_week"),
            row.get(f"course_{idx}_risk"), row.get(f"course_{idx}_priority"), row.get(f"course_{idx}_expected"),
            row.get(f"course_{idx}_completed"), row.get(f"course_{idx}_pending"), row.get(f"course_{idx}_inactivity_hours"),
            row.get(f"course_{idx}_average"), row.get(f"course_{idx}_completion"),
            row.get(f"course_{idx}_last_activity_at") or ("Sin actividad registrada" if row.get(f"course_{idx}_inactivity_estimated") else None),
            row.get(f"course_{idx}_inactivity_source"), row.get(f"course_{idx}_inactivity_reference_at"),
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(idx + 1, col, value)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    widths = [38, 24, 10, 12, 14, 11, 12, 11, 16, 14, 13, 25, 38, 26]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def _add_pending_sheet(wb: Workbook, row: dict[str, Any]) -> None:
    ws = wb.create_sheet("Actividades pendientes")
    headers = ["Curso", "Sección", "No.", "Actividad pendiente"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(1, col, header)
        cell.font = Font(bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.border = BORDER
    current = 2
    for idx in (1, 2):
        pending = _as_list(row.get(f"course_{idx}_pending_assignments"))
        if not pending:
            values = [row.get(f"course_{idx}_name"), row.get(f"course_{idx}_section"), "", "Sin actividades pendientes registradas"]
            for col, value in enumerate(values, 1):
                ws.cell(current, col, value).border = BORDER
            current += 1
            continue
        for number, activity in enumerate(pending, 1):
            values = [row.get(f"course_{idx}_name"), row.get(f"course_{idx}_section"), number, activity]
            for col, value in enumerate(values, 1):
                cell = ws.cell(current, col, value)
                cell.border = BORDER
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            current += 1
    widths = [38, 24, 8, 75]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def create_individual_combined_referral(row: dict[str, Any], academic_advisor: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Derivación unificada"
    _style_title(
        ws,
        "FORMATO UNIFICADO DE DERIVACIÓN ACADÉMICA A BIENESTAR",
        f"AVE — Dos cursos | Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        6,
    )
    fields = [
        ("Nombre completo", row.get("student_name")),
        ("Carné", row.get("carne")),
        ("Correo", row.get("email")),
        ("Carrera", row.get("career") or "Pendiente de completar"),
        ("Asesor académico", academic_advisor),
        ("Asesor de bienestar", row.get("asesor_bienestar") or "Sin asignar"),
        ("Riesgo consolidado", row.get("overall_risk")),
        ("Prioridad consolidada", row.get("intervention_priority")),
        ("Cursos incluidos", f"{row.get('course_1_name')} + {row.get('course_2_name')}"),
        ("Actividades esperadas acumuladas", int(_number(row.get("expected_total")))),
        ("Actividades completadas acumuladas", int(_number(row.get("completed_total")))),
        ("Tareas no entregadas acumuladas", int(_number(row.get("pending_total")))),
        ("Avance acumulado", f"{_number(row.get('completion_combined')):.2f} %"),
        ("Promedio combinado", "Sin datos" if _missing(row.get("average_combined")) else f"{_number(row.get('average_combined')):.2f} %"),
        ("Suma de desconexión por curso", "Sin datos" if _missing(row.get("inactivity_total_hours")) else f"{_number(row.get('inactivity_total_hours')):.1f} h"),
    ]
    start = 4
    for offset, (label, value) in enumerate(fields):
        _label_value(ws, start + offset, label, value)

    reason_row = start + len(fields) + 1
    ws.merge_cells(start_row=reason_row, start_column=1, end_row=reason_row, end_column=6)
    ws.cell(reason_row, 1, "RAZÓN CONSOLIDADA DE LA DERIVACIÓN")
    ws.cell(reason_row, 1).font = Font(bold=True, color=WHITE)
    ws.cell(reason_row, 1).fill = PatternFill("solid", fgColor=BLUE)
    ws.cell(reason_row, 1).alignment = Alignment(horizontal="center")
    ws.merge_cells(start_row=reason_row + 1, start_column=1, end_row=reason_row + 4, end_column=6)
    reason_cell = ws.cell(reason_row + 1, 1, build_combined_reason(row))
    reason_cell.alignment = Alignment(wrap_text=True, vertical="top")
    reason_cell.border = BORDER

    _label_value(ws, reason_row + 6, "Observaciones", row.get("referral_notes") or "")
    for col, width in {"A": 30, "B": 20, "C": 20, "D": 20, "E": 20, "F": 20}.items():
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    _add_course_detail_sheet(wb, row)
    _add_pending_sheet(wb, row)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def create_advisor_combined_report(group: pd.DataFrame, advisor: str, academic_advisor: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Listado unificado"
    _style_title(
        ws,
        f"DERIVACIONES UNIFICADAS PARA {advisor.upper()}",
        f"Asesor académico: {academic_advisor} | Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        18,
    )
    headers = [
        "Carné", "Estudiante", "Correo", "Riesgo", "Prioridad", "Asesor bienestar",
        "Curso 1", "Sección 1", "Pendientes C1", "Desconexión C1 (h)",
        "Curso 2", "Sección 2", "Pendientes C2", "Desconexión C2 (h)",
        "Pendientes total", "Desconexión suma (h)", "Avance total (%)", "Razón consolidada",
    ]
    header_row = 4
    for col, header in enumerate(headers, 1):
        cell = ws.cell(header_row, col, header)
        cell.font = Font(bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_num, row in enumerate(group.to_dict(orient="records"), header_row + 1):
        values = [
            row.get("carne"), row.get("student_name"), row.get("email"), row.get("overall_risk"),
            row.get("intervention_priority"), row.get("asesor_bienestar"),
            row.get("course_1_name"), row.get("course_1_section"), row.get("course_1_pending"), row.get("course_1_inactivity_hours"),
            row.get("course_2_name"), row.get("course_2_section"), row.get("course_2_pending"), row.get("course_2_inactivity_hours"),
            row.get("pending_total"), row.get("inactivity_total_hours"), row.get("completion_combined"), build_combined_reason(row),
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(row_num, col, value)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=col in {2, 3, 7, 8, 11, 12, 18})
            if row.get("overall_risk") == "Alto":
                cell.fill = PatternFill("solid", fgColor=LIGHT_RED)
            elif row.get("overall_risk") == "Moderado":
                cell.fill = PatternFill("solid", fgColor=LIGHT_AMBER)

    end_row = header_row + len(group)
    if len(group):
        table = Table(displayName="TablaDerivacionesUnificadas", ref=f"A{header_row}:R{end_row}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False, showLastColumn=False, showColumnStripes=False)
        ws.add_table(table)
    widths = [13, 30, 28, 11, 14, 24, 34, 22, 14, 19, 34, 22, 14, 19, 15, 20, 16, 70]
    for idx, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A5"
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def generate_combined_referral_package(
    selected: pd.DataFrame,
    *,
    academic_advisor: str,
) -> tuple[bytes, list[dict[str, Any]]]:
    if selected is None or selected.empty:
        raise ValueError("No se seleccionaron estudiantes para derivación unificada.")

    buffer = io.BytesIO()
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as package:
        for advisor_name, group in selected.groupby("asesor_bienestar", dropna=False):
            advisor = str(advisor_name or "Sin asignar")
            folder = _safe_filename(advisor)
            report = create_advisor_combined_report(group, advisor, academic_advisor)
            package.writestr(f"{folder}/Informe_unificado.xlsx", report)

            for row in group.to_dict(orient="records"):
                individual = create_individual_combined_referral(row, academic_advisor)
                filename = f"Derivacion_Unificada_{_safe_filename(row.get('carne'))}_{_safe_filename(row.get('student_name'))}.xlsx"
                package.writestr(f"{folder}/{filename}", individual)
                records.append(
                    {
                        "carne": str(row.get("carne") or ""),
                        "canvas_user_id": str(row.get("canvas_user_id") or ""),
                        "student_name": row.get("student_name"),
                        "email": row.get("email"),
                        "course_id": f"{row.get('course_1_id')}|{row.get('course_2_id')}",
                        "course_name": f"{row.get('course_1_name')} + {row.get('course_2_name')}",
                        "section_name": f"{row.get('course_1_section')} | {row.get('course_2_section')}",
                        "week_number": max(int(_number(row.get("course_1_week"))), int(_number(row.get("course_2_week")))) or None,
                        "average_grade": row.get("average_combined"),
                        "completion_percentage": row.get("completion_combined"),
                        "advisor_name": advisor,
                        "risk_level": row.get("overall_risk"),
                        "priority": row.get("intervention_priority"),
                        "reason": build_combined_reason(row),
                        "status": "generated",
                    }
                )
    return buffer.getvalue(), records
