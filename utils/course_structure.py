from __future__ import annotations

MODULE_COUNT = 3
WEEKS_PER_MODULE = 7
TOTAL_WEEKS = MODULE_COUNT * WEEKS_PER_MODULE


def global_week(module_number: int, module_week: int) -> int:
    module = min(max(1, int(module_number)), MODULE_COUNT)
    week = min(max(1, int(module_week)), WEEKS_PER_MODULE)
    return (module - 1) * WEEKS_PER_MODULE + week


def split_global_week(week_number: int) -> tuple[int, int]:
    safe = min(max(1, int(week_number)), TOTAL_WEEKS)
    return ((safe - 1) // WEEKS_PER_MODULE + 1, (safe - 1) % WEEKS_PER_MODULE + 1)


def week_label(week_number: int, *, include_global: bool = False) -> str:
    module, week = split_global_week(week_number)
    label = f"Módulo {module} · Semana {week}"
    return f"{label} (semana global {int(week_number)})" if include_global else label


def period_label(week_number: int, total_weeks: int | None = None) -> str:
    if int(total_weeks or TOTAL_WEEKS) == TOTAL_WEEKS:
        return week_label(week_number)
    return f"Semana {int(week_number)} de {int(total_weeks or TOTAL_WEEKS)}"
