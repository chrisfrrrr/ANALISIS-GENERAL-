from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import threading
import time
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class CanvasAPIError(RuntimeError):
    """Error legible para el usuario al consultar la API de Canvas."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(slots=True)
class CanvasConnectionResult:
    ok: bool
    message: str
    profile: dict[str, Any] | None = None


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    if not values:
        return [[]]
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


class CanvasService:
    """Cliente robusto para la API REST de Canvas.

    Además de reintentar errores transitorios, coordina las llamadas que usan el
    mismo token para evitar que varios hilos consuman simultáneamente el límite
    dinámico de Canvas. Esto es especialmente importante al consultar Page Views.
    """

    _registry_lock = threading.Lock()
    _scope_locks: dict[str, threading.RLock] = {}
    _rate_state: dict[str, dict[str, float | None]] = {}

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int | tuple[int, int] = (15, 120),
        *,
        rate_limit_retries: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "AVE-Alerta-Temprana/2.2",
            }
        )

        # Los reintentos de estado HTTP se manejan manualmente para poder respetar
        # el 429 de Canvas y compartir el tiempo de enfriamiento entre instancias.
        retry_policy = Retry(
            total=2,
            connect=2,
            read=2,
            status=0,
            backoff_factor=0.5,
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_policy, pool_connections=12, pool_maxsize=12)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        scope_material = f"{self.base_url}|{self.token}".encode("utf-8", errors="ignore")
        self._rate_scope = sha256(scope_material).hexdigest()
        with self._registry_lock:
            self._rate_lock = self._scope_locks.setdefault(self._rate_scope, threading.RLock())
            self._rate_state.setdefault(
                self._rate_scope,
                {"next_request_at": 0.0, "remaining": None, "request_cost": None},
            )

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    @staticmethod
    def _header_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _retry_after_seconds(value: Any) -> float | None:
        seconds = CanvasService._header_float(value)
        if seconds is None:
            return None
        return max(0.0, seconds)

    @staticmethod
    def _pacing_delay(remaining: float | None) -> float:
        """Pequeña pausa preventiva según el presupuesto informado por Canvas."""
        if remaining is None:
            return 0.04
        if remaining <= 5:
            return 4.0
        if remaining <= 10:
            return 2.0
        if remaining <= 20:
            return 1.0
        if remaining <= 40:
            return 0.35
        if remaining <= 80:
            return 0.12
        return 0.04

    @property
    def rate_limit_remaining(self) -> float | None:
        with self._rate_lock:
            return self._rate_state[self._rate_scope].get("remaining")

    def _wait_for_shared_slot(self) -> None:
        state = self._rate_state[self._rate_scope]
        wait_seconds = max(0.0, float(state.get("next_request_at") or 0.0) - time.monotonic())
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _observe_rate_headers(self, response: requests.Response) -> None:
        state = self._rate_state[self._rate_scope]
        remaining = self._header_float(response.headers.get("X-Rate-Limit-Remaining"))
        request_cost = self._header_float(response.headers.get("X-Request-Cost"))
        if remaining is not None:
            state["remaining"] = remaining
        if request_cost is not None:
            state["request_cost"] = request_cost
        state["next_request_at"] = max(
            float(state.get("next_request_at") or 0.0),
            time.monotonic() + self._pacing_delay(remaining),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        data: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int | tuple[int, int] | None = None,
        rate_limit_retries: int | None = None,
    ) -> requests.Response:
        if not self.token:
            raise CanvasAPIError("Debe ingresar un token de Canvas.")
        if not self.base_url.startswith(("https://", "http://")):
            raise CanvasAPIError("La URL de Canvas no es válida.")

        safe_method = method.upper() in {"GET", "HEAD", "OPTIONS"}
        allowed_429_retries = self.rate_limit_retries if rate_limit_retries is None else max(0, int(rate_limit_retries))
        rate_attempts = 0
        server_attempts = 0

        while True:
            # Todas las instancias con el mismo token comparten esta compuerta. Así
            # Page Views no compite contra inscripciones/actividades/entregas.
            with self._rate_lock:
                self._wait_for_shared_slot()
                try:
                    response = self.session.request(
                        method,
                        self._url(path),
                        params=params,
                        data=data,
                        json=json,
                        timeout=timeout or self.timeout,
                    )
                except requests.exceptions.ReadTimeout as exc:
                    raise CanvasAPIError(
                        "Canvas tardó demasiado en responder. La aplicación amplió el tiempo de espera; "
                        "vuelva a ejecutar el análisis si el problema persiste."
                    ) from exc
                except requests.exceptions.ConnectTimeout as exc:
                    raise CanvasAPIError(
                        "No fue posible establecer conexión con Canvas dentro del tiempo esperado. Intente nuevamente."
                    ) from exc
                except requests.exceptions.ConnectionError as exc:
                    raise CanvasAPIError(
                        "No fue posible comunicarse con Canvas. Verifique la conexión a internet y vuelva a intentarlo."
                    ) from exc
                except requests.RequestException as exc:
                    raise CanvasAPIError("Canvas no pudo completar la solicitud en este momento.") from exc

                self._observe_rate_headers(response)

                if response.status_code == 429 and safe_method and rate_attempts < allowed_429_retries:
                    retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                    # Si Canvas no especifica Retry-After, se usa espera exponencial.
                    delay = retry_after if retry_after is not None else min(30.0, 2.0 * (2 ** rate_attempts))
                    self._rate_state[self._rate_scope]["next_request_at"] = max(
                        float(self._rate_state[self._rate_scope].get("next_request_at") or 0.0),
                        time.monotonic() + delay,
                    )
                    rate_attempts += 1
                    continue

                if response.status_code >= 500 and safe_method and server_attempts < 2:
                    delay = min(6.0, 1.5 * (2 ** server_attempts))
                    self._rate_state[self._rate_scope]["next_request_at"] = max(
                        float(self._rate_state[self._rate_scope].get("next_request_at") or 0.0),
                        time.monotonic() + delay,
                    )
                    server_attempts += 1
                    continue

            break

        if response.status_code >= 400:
            detail = response.text[:400]
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("errors") or payload.get("message") or payload)
            except ValueError:
                pass

            if response.status_code in {401, 403}:
                raise CanvasAPIError(
                    f"Canvas rechazó la solicitud ({response.status_code}). Revise el token y los permisos asignados.",
                    status_code=response.status_code,
                )
            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                extra = f" Canvas indicó esperar aproximadamente {int(round(retry_after))} s." if retry_after else ""
                raise CanvasAPIError(
                    "Canvas sigue limitando temporalmente las consultas después de los reintentos automáticos."
                    f"{extra} La aplicación conservará los análisis de cursos que ya logró completar para no repetirlos.",
                    status_code=429,
                    retry_after=retry_after,
                )
            if response.status_code >= 500:
                raise CanvasAPIError(
                    "Canvas presentó una interrupción temporal al procesar la consulta. Vuelva a intentarlo en unos minutos.",
                    status_code=response.status_code,
                )
            raise CanvasAPIError(
                f"Canvas no pudo completar la consulta ({response.status_code}): {detail}",
                status_code=response.status_code,
            )
        return response

    def get(
        self,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        *,
        rate_limit_retries: int | None = None,
    ) -> Any:
        return self._request("GET", path, params=params, rate_limit_retries=rate_limit_retries).json()

    def get_paginated(
        self,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        max_pages: int = 100,
        *,
        rate_limit_retries: int | None = None,
    ) -> list[Any]:
        items: list[Any] = []
        url = self._url(path)
        current_params = params
        pages = 0
        visited: set[str] = set()

        while url and pages < max_pages:
            if url in visited:
                break
            visited.add(url)
            response = self._request(
                "GET",
                url,
                params=current_params,
                rate_limit_retries=rate_limit_retries,
            )
            payload = response.json()
            if isinstance(payload, list):
                items.extend(payload)
            elif isinstance(payload, dict):
                items.append(payload)
            else:
                break
            url = response.links.get("next", {}).get("url")
            current_params = None
            pages += 1
        return items

    def test_connection(self) -> CanvasConnectionResult:
        try:
            profile = self.get("/api/v1/users/self/profile")
            name = profile.get("name") or profile.get("short_name") or "usuario"
            return CanvasConnectionResult(True, f"Conexión correcta como {name}.", profile)
        except CanvasAPIError as exc:
            return CanvasConnectionResult(False, str(exc), None)

    def list_courses(self) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("per_page", 100),
            ("state[]", "available"),
            ("state[]", "completed"),
            ("include[]", "term"),
            ("include[]", "total_students"),
            ("include[]", "sections"),
        ]
        courses = self.get_paginated("/api/v1/courses", params=params)
        return [course for course in courses if isinstance(course, dict) and course.get("id")]

    def list_sections(self, course_id: int | str) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("per_page", 100), ("include[]", "total_students")]
        return self.get_paginated(f"/api/v1/courses/{course_id}/sections", params=params)

    def list_enrollments(
        self,
        course_id: int | str,
        section_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        if section_id:
            path = f"/api/v1/sections/{section_id}/enrollments"
        else:
            path = f"/api/v1/courses/{course_id}/enrollments"
        params: list[tuple[str, Any]] = [
            ("per_page", 100),
            ("type[]", "StudentEnrollment"),
            ("state[]", "active"),
            ("include[]", "total_scores"),
            ("include[]", "avatar_url"),
        ]
        return self.get_paginated(path, params=params)

    def list_course_students(self, course_id: int | str) -> list[dict[str, Any]]:
        """Obtiene el directorio de estudiantes con identificadores institucionales."""
        params: list[tuple[str, Any]] = [
            ("per_page", 100),
            ("enrollment_type[]", "student"),
            ("enrollment_state[]", "active"),
            ("include[]", "email"),
            ("include[]", "enrollments"),
            ("include[]", "avatar_url"),
        ]
        return self.get_paginated(f"/api/v1/courses/{course_id}/users", params=params)

    def list_assignments(self, course_id: int | str) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("per_page", 100),
            ("include[]", "all_dates"),
            ("order_by", "due_at"),
        ]
        return self.get_paginated(f"/api/v1/courses/{course_id}/assignments", params=params)

    def _submission_batch(
        self,
        path: str,
        student_ids: Sequence[str],
        assignment_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("per_page", 50),
            ("grouped", "true"),
            ("enrollment_state", "active"),
        ]
        for student_id in student_ids:
            params.append(("student_ids[]", student_id))
        for assignment_id in assignment_ids:
            params.append(("assignment_ids[]", assignment_id))
        return self.get_paginated(path, params=params, max_pages=25)

    def list_submissions(
        self,
        course_id: int | str,
        section_id: int | str | None = None,
        *,
        student_ids: Iterable[int | str] | None = None,
        assignment_ids: Iterable[int | str] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Obtiene entregas en lotes pequeños para reducir tiempos y costo API."""
        if section_id:
            path = f"/api/v1/sections/{section_id}/students/submissions"
        else:
            path = f"/api/v1/courses/{course_id}/students/submissions"

        students = [str(value) for value in (student_ids or []) if str(value)]
        assignments = [str(value) for value in (assignment_ids or []) if str(value)]
        if not students:
            students = ["all"]

        student_batches = _chunks(students, 25) if students != ["all"] else [["all"]]
        assignment_batches = _chunks(assignments, 40) if assignments else [[]]
        total_batches = len(student_batches) * len(assignment_batches)
        completed_batches = 0
        results: list[dict[str, Any]] = []

        for student_batch in student_batches:
            for assignment_batch in assignment_batches:
                results.extend(self._submission_batch(path, student_batch, assignment_batch))
                completed_batches += 1
                if progress_callback:
                    progress_callback(completed_batches, total_batches)
        return results

    def list_page_views(
        self,
        user_id: int | str,
        start_time: datetime,
        end_time: datetime,
        *,
        max_pages: int = 2,
        rate_limit_retries: int = 0,
    ) -> list[dict[str, Any]]:
        params = {
            "per_page": 100,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        }
        # Page Views es complementario. Se limita a 200 vistas y no insiste ante
        # 429 para proteger las consultas esenciales de tareas y entregas.
        return self.get_paginated(
            f"/api/v1/users/{user_id}/page_views",
            params=params,
            max_pages=max_pages,
            rate_limit_retries=rate_limit_retries,
        )

    def send_message(
        self,
        recipient_ids: list[int | str],
        subject: str,
        body: str,
        *,
        force_new: bool = True,
    ) -> list[dict[str, Any]]:
        if not recipient_ids:
            raise CanvasAPIError("No se seleccionaron destinatarios.")
        data: list[tuple[str, Any]] = [("subject", subject), ("body", body)]
        data.append(("force_new", str(force_new).lower()))
        data.append(("group_conversation", "false"))
        for recipient in recipient_ids:
            data.append(("recipients[]", str(recipient)))
        payload = self._request("POST", "/api/v1/conversations", data=data).json()
        if isinstance(payload, list):
            return payload
        return [payload]

    def get_conversation(self, conversation_id: int | str) -> dict[str, Any]:
        return self.get(f"/api/v1/conversations/{conversation_id}", params={"include_all_conversation_ids": "true"})

    def count_sessions(
        self,
        page_views: list[dict[str, Any]],
        *,
        inactivity_gap_minutes: int = 30,
        course_id: int | str | None = None,
    ) -> int:
        timestamps: list[datetime] = []
        course_token = f"/courses/{course_id}/" if course_id else None
        for view in page_views:
            url = str(view.get("url") or "")
            if course_token and course_token not in url:
                context_id = str(view.get("context_id") or "")
                if context_id != str(course_id):
                    continue
            value = view.get("created_at")
            if not value:
                continue
            try:
                timestamps.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                continue
        if not timestamps:
            return 0
        timestamps.sort()
        sessions = 1
        for previous, current in zip(timestamps, timestamps[1:]):
            if (current - previous).total_seconds() > inactivity_gap_minutes * 60:
                sessions += 1
        return sessions

    def fetch_page_view_sessions(
        self,
        user_ids: list[int | str],
        start_time: datetime,
        end_time: datetime,
        course_id: int | str,
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        max_workers: int = 2,
    ) -> tuple[dict[str, int | None], dict[str, str]]:
        """Estima sesiones sin permitir que Page Views agote el cupo de Canvas.

        ``max_workers`` se conserva por compatibilidad, pero las consultas se hacen
        de forma coordinada/secuencial porque Canvas aplica penalización preventiva
        cuando muchas solicitudes costosas salen en paralelo. Si el presupuesto
        baja o aparece el primer 429/403, se omite Page Views para los restantes.
        Las horas de desconexión siguen disponibles mediante ``last_activity_at``.
        """
        sessions: dict[str, int | None] = {}
        errors: dict[str, str] = {}
        normalized_ids = list(dict.fromkeys(str(value) for value in user_ids if str(value)))
        total = len(normalized_ids)
        if not total:
            return sessions, errors

        stop_reason: str | None = None
        completed = 0

        for index, user_id in enumerate(normalized_ids):
            remaining = self.rate_limit_remaining
            if remaining is not None and remaining <= 20:
                stop_reason = (
                    "Page Views se omitió para proteger el límite de Canvas; "
                    "las horas de desconexión se mantienen con la última actividad reportada."
                )
                break

            try:
                views = self.list_page_views(
                    user_id,
                    start_time,
                    end_time,
                    max_pages=2,
                    rate_limit_retries=0,
                )
                sessions[user_id] = self.count_sessions(views, course_id=course_id)
            except CanvasAPIError as exc:
                sessions[user_id] = None
                errors[user_id] = str(exc)
                if exc.status_code in {401, 403, 429}:
                    if exc.status_code == 429:
                        stop_reason = (
                            "Canvas alcanzó temporalmente su límite durante Page Views. "
                            "Se omitieron las consultas restantes para que continúe el análisis principal."
                        )
                    else:
                        stop_reason = (
                            "Canvas no permite consultar Page Views con este usuario/token. "
                            "Se continuará usando la última actividad disponible."
                        )
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)
                    break
            except Exception as exc:  # Page Views nunca debe abortar el análisis.
                sessions[user_id] = None
                errors[user_id] = f"Consulta de Page Views omitida: {exc}"

            completed += 1
            if progress_callback:
                progress_callback(completed, total)

        if stop_reason:
            for user_id in normalized_ids[completed:]:
                sessions.setdefault(user_id, None)
                errors.setdefault(user_id, stop_reason)
            if progress_callback and completed < total:
                progress_callback(total, total)

        return sessions, errors
