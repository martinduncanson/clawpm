"""FastAPI server for the ClawPM web UI (CLAWP-078: read-only dashboard).

The web layer is a *read-only* view over the portfolio. It exposes the same
data the CLI reads (projects, tasks, blockers, work-log) but performs no
mutations. State changes go through the CLI, which is the single, tested,
calibration-aware write path.

Why read-only (the CLAWP-078 decision): the previous web layer mutated state
through unguarded side-doors — `create_issue` hand-rolled `.agent/issues.jsonl`
appends (re-introducing the exact non-atomic corruption `append_jsonl_line`
fixed in CLAWP-032, with a drifted schema), `create_task` accepted no
predictions/success-criteria (bypassing the calibration discipline the CLI
enforces), and pause/resume did naive string-replace on `settings.toml`. None
of it was tested. Demoting to read-only removes that liability now; individual
write routes can be hardened behind tests and routed through the CLI's core
functions later if a real need arises (the `respond` route — already lock-safe
— is the strongest first candidate).

Response contract:
  * Success: the resource is returned directly (JSON list/object, or HTML for
    ``GET /``) with HTTP 200.
  * Error: a single consistent envelope ``{"error": {"code", "message"}}`` with
    the appropriate status code (400/404/405/500). This applies uniformly to
    errors raised by handlers, routing errors (unknown path -> 404, wrong method
    -> 405), request-validation failures (-> 400) and unexpected exceptions
    (-> 500).

Binds 127.0.0.1 only (see the ``serve`` CLI command); it is a local dashboard,
not a network service.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .discovery import load_portfolio_config, discover_projects, get_project
from .tasks import list_tasks
from .worklog import tail_entries


logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


_DEFAULT_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    500: "internal_error",
}

READ_ONLY_MESSAGE = (
    "The ClawPM web UI is read-only. Use the `clawpm` CLI to change state "
    "(it is the tested, calibration-aware write path)."
)


class ApiError(HTTPException):
    """HTTPException carrying a structured ``{code, message}`` detail so the
    exception handler can emit the consistent error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def _error_body(status_code: int, detail: object) -> dict:
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return {"error": {"code": detail["code"], "message": detail["message"]}}
    code = _DEFAULT_CODES.get(status_code, "error")
    return {"error": {"code": code, "message": str(detail)}}


def _read_only() -> None:
    """Raise the uniform read-only 405 for a demoted mutating route."""
    raise ApiError(405, "read_only", READ_ONLY_MESSAGE)


def create_app() -> FastAPI:
    app = FastAPI(title="ClawPM")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- Consistent error envelope for every failure mode -----------------

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Forward exc.headers so standard method-discovery headers (e.g. the
        # `Allow` header on a routing 405) survive the envelope reshaping.
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.status_code, exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "validation_error", "message": "invalid request body"}},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Fail loud on the server (full traceback to the `clawpm serve` console)
        # while keeping the client-facing envelope opaque — fail-open must not
        # mean fail-silent.
        logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "internal server error"}},
        )

    # ---- Reads ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        index_file = TEMPLATES_DIR / "index.html"
        return index_file.read_text(encoding="utf-8") if index_file.exists() else "<h1>ClawPM</h1>"

    @app.get("/api/projects")
    def api_projects() -> list[dict]:
        config = load_portfolio_config()
        return [p.to_dict() for p in discover_projects(config)]

    @app.get("/api/projects/{project_id}")
    def api_project_context(project_id: str) -> dict:
        config = load_portfolio_config()
        project = get_project(config, project_id)
        if not project:
            raise ApiError(404, "not_found", f"project '{project_id}' not found")
        return project.to_dict()

    @app.get("/api/projects/{project_id}/tasks")
    def api_project_tasks(project_id: str, state: str | None = None) -> list[dict]:
        config = load_portfolio_config()
        if not get_project(config, project_id):
            raise ApiError(404, "not_found", f"project '{project_id}' not found")
        from .models import TaskState

        state_filter = None
        if state:
            try:
                state_filter = TaskState(state)
            except ValueError:
                raise ApiError(400, "bad_request", f"invalid state '{state}'")
        tasks = list_tasks(config, project_id, state_filter=state_filter)
        return [t.to_dict() for t in tasks]

    @app.get("/api/blockers")
    def api_blockers() -> list[dict]:
        config = load_portfolio_config()
        from .models import TaskState

        blockers = []
        for proj in discover_projects(config):
            if not proj.project_dir:
                continue
            for task in list_tasks(config, proj.id, state_filter=TaskState.BLOCKED):
                blockers.append({"project": proj.id, "task": task.to_dict()})
        return blockers

    @app.get("/api/active-tasks")
    def api_active_tasks() -> list[dict]:
        config = load_portfolio_config()
        from .models import TaskState

        active = []
        for proj in discover_projects(config):
            if not proj.project_dir:
                continue
            for state in (TaskState.OPEN, TaskState.PROGRESS):
                for task in list_tasks(config, proj.id, state_filter=state):
                    active.append(
                        {"project": proj.id, "project_name": proj.name, "task": task.to_dict()}
                    )
        return active

    @app.get("/api/worklog")
    def api_worklog(project: str | None = None, limit: int = 10) -> list[dict]:
        config = load_portfolio_config()
        entries = tail_entries(config, project=project, limit=limit)
        return [e.to_dict() for e in entries]

    # ---- Demoted mutating routes (read-only: return 405) ------------------
    #
    # Registered so a client that still POSTs gets a clear, uniform 405
    # read-only envelope rather than an ambiguous 404. Re-home write-back on
    # these paths only behind tests, through the CLI's core functions.

    @app.post("/api/tasks/{project_id}/{task_id}/state")
    def api_change_task_state(project_id: str, task_id: str) -> None:
        _read_only()

    @app.post("/api/tasks/{project_id}/{task_id}/respond")
    def api_respond_to_task(project_id: str, task_id: str) -> None:
        _read_only()

    @app.post("/api/log")
    def api_add_log_entry() -> None:
        _read_only()

    @app.post("/api/tasks")
    def api_create_task() -> None:
        _read_only()

    @app.post("/api/projects/{project_id}/pause")
    def api_pause_project(project_id: str) -> None:
        _read_only()

    @app.post("/api/projects/{project_id}/resume")
    def api_resume_project(project_id: str) -> None:
        _read_only()

    @app.post("/api/issues")
    def api_create_issue() -> None:
        _read_only()

    return app
