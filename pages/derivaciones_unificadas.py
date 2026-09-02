from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from components.ui import empty_state, metric_card, page_header
from models.config import RiskConfig
from services.analysis_service import AnalysisService
from services.auth_service import current_actor, get_valid_canvas_token, load_oauth_config
from services.canvas_service import CanvasAPIError, CanvasService
from services.combined_referral_service import combine_course_analyses, generate_combined_referral_package
from services.database_service import DatabaseError
from services.demo_service import demo_courses, demo_sections, generate_demo_analysis
from services.runtime import get_database
from utils.course_structure import MODULE_COUNT, WEEKS_PER_MODULE, global_week, period_label
from utils.data_cleaning import load_wellbeing_csv, merge_wellbeing

ROOT = Path(__file__).resolve().parents[1]
WELLBEING_PATH = ROOT / "data" / "bienestar_base.csv"

page_header(
    "Derivación unificada · dos cursos",
    "Seleccione un par de cursos y las secciones de cada uno. La app consolida por estudiante pendientes, desconexión y riesgo, conservando el detalle individual de ambos cursos.",
)

config = RiskConfig.from_dict(st.session_state.get("risk_config"))
db = get_database()
demo_mode = bool(st.session_state.get("demo_mode", True))
oauth_config = load_oauth_config()
canvas_url = st.session_state.get("canvas_url", oauth_config.canvas_url)
token = st.session_state.get("canvas_token", "")
if not demo_mode:
    try:
        token = get_valid_canvas_token(oauth_config)
    except Exception:
        token = st.session_state.get("canvas_token", "")

courses = st.session_state.get("courses") or (demo_courses() if demo_mode else [])
if len(courses) < 2:
    empty_state(
        "Se necesitan al menos dos cursos",
        "Cargue los cursos desde Conexión y análisis antes de preparar una derivación unificada.",
    )
    st.stop()

course_labels = {
    f"{course.get('name') or course.get('course_code')} · ID {course.get('id')}": course
    for course in courses
}
labels = list(course_labels.keys())

st.subheader("1. Seleccione los dos cursos")
c1, c2 = st.columns(2)
with c1:
    label_1 = st.selectbox("Curso 1", labels, key="combined_course_1")
    course_1 = course_labels[label_1]
with c2:
    remaining_labels = [label for label in labels if label != label_1]
    label_2 = st.selectbox("Curso 2", remaining_labels, key="combined_course_2")
    course_2 = course_labels[label_2]


def get_sections(course: dict) -> list[dict]:
    key = f"sections_{course['id']}_{'demo' if demo_mode else 'real'}"
    if key not in st.session_state:
        if demo_mode:
            st.session_state[key] = demo_sections(course["id"])
        else:
            canvas = CanvasService(canvas_url, token)
            st.session_state[key] = canvas.list_sections(course["id"])
    return st.session_state.get(key, [])


try:
    sections_1 = get_sections(course_1)
    sections_2 = get_sections(course_2)
except CanvasAPIError as exc:
    st.error(f"No fue posible cargar las secciones: {exc}")
    st.stop()


def section_options(sections: list[dict]) -> dict[str, dict]:
    return {
        f"{item.get('name') or 'Sección'} ({item.get('total_students', '—')} estudiantes)": item
        for item in sections
        if item.get("id") is not None
    }


options_1 = section_options(sections_1)
options_2 = section_options(sections_2)

st.subheader("2. Elija cuántas secciones incluir de cada curso")
s1, s2 = st.columns(2)
with s1:
    selected_labels_1 = st.multiselect(
        f"Secciones de {course_1.get('name')}",
        list(options_1.keys()),
        default=list(options_1.keys()),
        key=f"combined_sections_{course_1['id']}",
    )
    selected_sections_1 = [options_1[label] for label in selected_labels_1]
    st.caption(f"Se incluirán {len(selected_sections_1)} sección(es).")
with s2:
    selected_labels_2 = st.multiselect(
        f"Secciones de {course_2.get('name')}",
        list(options_2.keys()),
        default=list(options_2.keys()),
        key=f"combined_sections_{course_2['id']}",
    )
    selected_sections_2 = [options_2[label] for label in selected_labels_2]
    st.caption(f"Se incluirán {len(selected_sections_2)} sección(es).")

st.subheader("3. Defina el corte de cada curso")
w1, w2, cutoff_col = st.columns([1.55, 1.55, 1.1])
with w1:
    m1a, m1b = st.columns(2)
    module_1 = m1a.selectbox("Módulo · Curso 1", range(1, MODULE_COUNT + 1), key="combined_module_1")
    module_week_1 = m1b.selectbox("Semana · Curso 1", range(1, WEEKS_PER_MODULE + 1), key="combined_week_1")
    week_1 = global_week(module_1, module_week_1)
    st.caption(period_label(week_1, config.course_weeks))
with w2:
    m2a, m2b = st.columns(2)
    module_2 = m2a.selectbox("Módulo · Curso 2", range(1, MODULE_COUNT + 1), key="combined_module_2")
    module_week_2 = m2b.selectbox("Semana · Curso 2", range(1, WEEKS_PER_MODULE + 1), key="combined_week_2")
    week_2 = global_week(module_2, module_week_2)
    st.caption(period_label(week_2, config.course_weeks))
with cutoff_col:
    analysis_date = st.date_input("Fecha de corte", value=date.today(), key="combined_cutoff")

opt1, opt2 = st.columns(2)
with opt1:
    include_page_views = st.toggle(
        "Estimar ingresos con Page Views",
        value=False,
        disabled=demo_mode,
        key="combined_page_views",
        help="Puede aumentar el tiempo de consulta. La desconexión por última actividad seguirá disponible cuando Canvas la reporte.",
    )
with opt2:
    include_zero_point = st.toggle(
        "Incluir actividades de 0 puntos",
        value=False,
        key="combined_zero_points",
    )


def activity_plan_for(course_id: int | str) -> pd.DataFrame:
    session_plan = st.session_state.get(f"course_activity_plan_records_{course_id}")
    if session_plan:
        return pd.DataFrame(session_plan)
    if not demo_mode and db.connected:
        return db.get_course_activity_plan(course_id)
    return pd.DataFrame()


current_signature = (
    str(course_1["id"]),
    tuple(sorted(str(item["id"]) for item in selected_sections_1)),
    int(week_1),
    str(course_2["id"]),
    tuple(sorted(str(item["id"]) for item in selected_sections_2)),
    int(week_2),
    analysis_date.isoformat(),
    bool(include_page_views),
    bool(include_zero_point),
)

can_analyze = bool(selected_sections_1 and selected_sections_2)
if not can_analyze:
    st.warning("Seleccione al menos una sección en cada curso para ejecutar el análisis unificado.")

if st.button(
    "Ejecutar análisis de los dos cursos",
    type="primary",
    width="stretch",
    disabled=not can_analyze,
):
    progress = st.progress(0, text="Preparando análisis combinado...")

    def scaled_progress(course_number: int, label: str, value: float) -> None:
        base = 0.0 if course_number == 1 else 0.5
        progress.progress(min(0.99, base + 0.48 * min(max(value, 0.0), 1.0)), text=f"Curso {course_number}: {label}")

    try:
        latest_messages = db.get_latest_messages() if db.connected else pd.DataFrame()
        wellbeing = load_wellbeing_csv(WELLBEING_PATH)
        frames: list[pd.DataFrame] = []
        diagnostics: list[dict] = []

        for slot, (course, selected_sections, week) in enumerate(
            [(course_1, selected_sections_1, week_1), (course_2, selected_sections_2, week_2)],
            start=1,
        ):
            section_ids = [item["id"] for item in selected_sections]
            section_name_map = {str(item["id"]): item.get("name") or "Sección" for item in selected_sections}

            if demo_mode:
                demo_frames = []
                demo_diagnostics = []
                for index, section in enumerate(selected_sections, start=1):
                    progress.progress(
                        (0.0 if slot == 1 else 0.5) + 0.48 * index / max(len(selected_sections), 1),
                        text=f"Curso {slot}: generando {section.get('name')}",
                    )
                    frame, _detail, diag = generate_demo_analysis(
                        course=course,
                        section_id=section["id"],
                        section_name=section.get("name") or "Sección",
                        week=week,
                        analysis_date=analysis_date,
                        config=config,
                        wellbeing_path=WELLBEING_PATH,
                    )
                    demo_frames.append(frame)
                    demo_diagnostics.append(diag)
                dataframe = pd.concat(demo_frames, ignore_index=True) if demo_frames else pd.DataFrame()
                diag = {
                    "course_id": str(course["id"]),
                    "course_name": course.get("name"),
                    "students": len(dataframe),
                    "selected_sections": section_name_map,
                    "demo": True,
                    "section_diagnostics": demo_diagnostics,
                }
            else:
                previous_history = (
                    db.get_snapshot_history(course_id=course["id"], limit=5000) if db.connected else pd.DataFrame()
                )
                canvas = CanvasService(canvas_url, token)
                service = AnalysisService(canvas, config)
                dataframe, _detail, diag = service.analyze_course(
                    course=course,
                    section_id=None,
                    section_name="Selección múltiple",
                    section_ids=section_ids,
                    section_name_map=section_name_map,
                    week=week,
                    analysis_date=analysis_date,
                    include_page_views=include_page_views,
                    include_zero_point=include_zero_point,
                    latest_messages=latest_messages,
                    previous_history=previous_history,
                    activity_plan=activity_plan_for(course["id"]),
                    progress_callback=lambda label, value, n=slot: scaled_progress(n, label, value),
                )
                dataframe = merge_wellbeing(dataframe, wellbeing)
                dataframe["advisor_name"] = dataframe["asesor_bienestar"]

            frames.append(dataframe)
            diagnostics.append(diag)

        raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        combined = combine_course_analyses(raw, [course_1, course_2])
        st.session_state.combined_analysis_raw = raw
        st.session_state.combined_analysis_df = combined
        st.session_state.combined_analysis_diagnostics = diagnostics
        st.session_state.combined_courses = [course_1, course_2]
        st.session_state.combined_analysis_signature = current_signature
        st.session_state.combined_referral_package = None
        st.session_state.combined_referral_records = []
        progress.progress(1.0, text="Análisis unificado completado")
        progress.empty()
        st.success(
            f"Análisis completado: {len(combined)} estudiante(s) únicos entre los dos cursos. "
            f"Se revisaron {len(selected_sections_1)} sección(es) del Curso 1 y {len(selected_sections_2)} del Curso 2."
        )
    except (CanvasAPIError, DatabaseError, ValueError) as exc:
        progress.empty()
        st.error(str(exc))
    except Exception:
        import logging

        progress.empty()
        logging.exception("Error inesperado durante la derivación unificada")
        st.error("Ocurrió un inconveniente al analizar los dos cursos. Revise los logs de la aplicación para el detalle técnico.")

combined = st.session_state.get("combined_analysis_df")
if combined is None or combined.empty:
    st.stop()
if st.session_state.get("combined_analysis_signature") != current_signature:
    st.warning("Los parámetros de cursos, secciones o corte cambiaron. Ejecute nuevamente el análisis antes de generar derivaciones.")
    st.stop()

st.divider()
st.subheader("4. Resultado consolidado por estudiante")

m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    metric_card("Estudiantes únicos", len(combined), "Unificados entre ambos cursos")
with m2:
    metric_card("En ambos cursos", int((combined["courses_present"] == 2).sum()), "Coincidencia completa")
with m3:
    metric_card("Solo en un curso", int((combined["courses_present"] == 1).sum()), "Revisar matrícula/secciones")
with m4:
    metric_card("Pendientes acumulados", int(combined["pending_total"].sum()), "Suma Curso 1 + Curso 2")
with m5:
    metric_card("Riesgo alto", int((combined["overall_risk"] == "Alto").sum()), "Máximo riesgo entre cursos")

if combined.get("advisor_conflict", pd.Series(False, index=combined.index)).fillna(False).any():
    st.warning("Se detectaron estudiantes con asesores de bienestar distintos entre cursos. Estos casos se marcaron para revisión.")

eligible = combined[combined["overall_risk"].isin(["Moderado", "Alto"])].copy()
if eligible.empty:
    st.success("No existen estudiantes con riesgo moderado o alto para derivar en este corte.")
    st.stop()

f1, f2 = st.columns(2)
with f1:
    risk_filter = st.multiselect("Riesgo consolidado", ["Moderado", "Alto"], default=["Moderado", "Alto"], key="combined_risk_filter")
with f2:
    advisors = sorted(eligible["asesor_bienestar"].fillna("Sin asignar").unique().tolist())
    advisor_filter = st.multiselect("Asesor de bienestar", advisors, default=advisors, key="combined_advisor_filter")

filtered = eligible.copy()
if risk_filter:
    filtered = filtered[filtered["overall_risk"].isin(risk_filter)]
if advisor_filter:
    filtered = filtered[filtered["asesor_bienestar"].fillna("Sin asignar").isin(advisor_filter)]

show_columns = [
    "student_key", "carne", "student_name", "overall_risk", "intervention_priority", "asesor_bienestar",
    "course_1_name", "course_1_section", "course_1_pending", "course_1_inactivity_hours",
    "course_2_name", "course_2_section", "course_2_pending", "course_2_inactivity_hours",
    "pending_total", "inactivity_total_hours", "courses_present",
]
editor = filtered[show_columns].copy()
editor.insert(0, "Seleccionar", False)
editor = editor.rename(
    columns={
        "carne": "Carné",
        "student_name": "Estudiante",
        "overall_risk": "Riesgo",
        "intervention_priority": "Prioridad",
        "asesor_bienestar": "Asesor de bienestar",
        "course_1_name": "Curso 1",
        "course_1_section": "Sección C1",
        "course_1_pending": "Pendientes C1",
        "course_1_inactivity_hours": "Desconexión C1 (h)",
        "course_2_name": "Curso 2",
        "course_2_section": "Sección C2",
        "course_2_pending": "Pendientes C2",
        "course_2_inactivity_hours": "Desconexión C2 (h)",
        "pending_total": "Pendientes total",
        "inactivity_total_hours": "Desconexión suma (h)",
        "courses_present": "Cursos presentes",
    }
)

edited = st.data_editor(
    editor,
    width="stretch",
    hide_index=True,
    height=480,
    disabled=[column for column in editor.columns if column != "Seleccionar"],
    column_config={
        "Seleccionar": st.column_config.CheckboxColumn(required=True),
        "student_key": None,
    },
)
selected_keys = edited.loc[edited["Seleccionar"], "student_key"].astype(str).tolist()
selected = filtered[filtered["student_key"].astype(str).isin(selected_keys)].copy()

st.caption(
    "La columna ‘Desconexión suma’ corresponde a la suma referencial de las horas reportadas por cada curso; "
    "se conserva también cada valor individual para evitar perder contexto."
)

s1, s2, s3 = st.columns(3)
s1.metric("Seleccionados", len(selected))
s2.metric("Pendientes acumulados", int(selected["pending_total"].sum()) if not selected.empty else 0)
s3.metric("Asesores receptores", selected["asesor_bienestar"].nunique() if not selected.empty else 0)

academic_advisor = st.text_input(
    "Asesor académico que firma",
    value=st.session_state.get("academic_advisor", "Ing. Christian Pocol"),
    key="combined_academic_advisor",
)
st.session_state.academic_advisor = academic_advisor
notes = st.text_area(
    "Observaciones generales para el paquete unificado",
    placeholder="Opcional: contexto del corte, acciones previas o información que deba conocer Bienestar.",
    key="combined_referral_notes",
)

if not selected.empty and db.connected:
    recent = db.get_recent_referrals(selected["carne"].astype(str).tolist(), config.referral_cooldown_days)
    if not recent.empty:
        st.warning(
            f"Se encontraron {recent['carne'].astype(str).nunique()} estudiante(s) con derivación activa en los últimos "
            f"{config.referral_cooldown_days} días."
        )
        with st.expander("Ver posibles duplicados"):
            st.dataframe(recent, width="stretch", hide_index=True)

confirm = st.checkbox(
    "Confirmo que revisé la información de ambos cursos y las posibles derivaciones previas.",
    key="combined_referral_confirm",
)
if st.button(
    "Generar paquete unificado de derivaciones",
    type="primary",
    width="stretch",
    disabled=selected.empty or not confirm,
):
    try:
        if notes:
            selected["referral_notes"] = notes
        package, records = generate_combined_referral_package(selected, academic_advisor=academic_advisor)
        st.session_state.combined_referral_package = package
        st.session_state.combined_referral_records = records

        if db.connected:
            batch_id = db.save_referral_batch(
                {
                    "analysis_run_id": None,
                    "created_by_name": academic_advisor,
                    "student_count": len(records),
                    "advisor_count": selected["asesor_bienestar"].nunique(),
                    "notes": notes or "Derivación unificada de dos cursos",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            saved_count = db.save_referrals(batch_id, records)
            db.log_audit(
                action="combined_referral_package_generated",
                entity_type="referral_batch",
                entity_id=batch_id,
                actor=current_actor(),
                payload={
                    "student_count": saved_count,
                    "advisor_count": selected["asesor_bienestar"].nunique(),
                    "course_1_id": str(course_1["id"]),
                    "course_2_id": str(course_2["id"]),
                    "sections_course_1": [str(item["id"]) for item in selected_sections_1],
                    "sections_course_2": [str(item["id"]) for item in selected_sections_2],
                },
            )
        st.success("Paquete unificado generado correctamente.")
    except (ValueError, DatabaseError) as exc:
        st.error(str(exc))

if st.session_state.get("combined_referral_package"):
    st.download_button(
        "Descargar paquete unificado ZIP",
        data=st.session_state.combined_referral_package,
        file_name=f"Derivaciones_Unificadas_AVE_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
        mime="application/zip",
        type="primary",
        width="stretch",
    )
    st.caption(
        "El ZIP contiene una carpeta por asesora/or de bienestar, un informe unificado y un Excel individual por estudiante con detalle por curso y actividades pendientes."
    )
