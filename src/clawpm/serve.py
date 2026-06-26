"""FastAPI server for ClawPM web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .discovery import load_portfolio_config, discover_projects, get_project
from .tasks import list_tasks, get_task, change_task_state
from .worklog import add_entry, tail_entries


WEB_DIR = Path(__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="ClawPM")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        index_file = TEMPLATES_DIR / "index.html"
        return index_file.read_text(encoding="utf-8") if index_file.exists() else "<h1>ClawPM</h1>"

    @app.get("/api/projects")
    def api_projects() -> list[dict]:
        config = load_portfolio_config()
        if not config:
            return []
        projects = discover_projects(config)
        return [p.to_dict() for p in projects]

    @app.get("/api/projects/{project_id}")
    def api_project_context(project_id: str) -> dict | None:
        config = load_portfolio_config()
        if not config:
            return None
        project = get_project(config, project_id)
        return project.to_dict() if project else None

    @app.get("/api/projects/{project_id}/tasks")
    def api_project_tasks(project_id: str, state: str | None = None) -> list[dict]:
        config = load_portfolio_config()
        if not config:
            return []
        from .models import TaskState
        state_filter = TaskState(state) if state else None
        tasks = list_tasks(config, project_id, state_filter=state_filter)
        return [t.to_dict() for t in tasks]

    @app.get("/api/blockers")
    def api_blockers() -> list[dict]:
        config = load_portfolio_config()
        if not config:
            return []
        from .models import TaskState
        blockers = []
        projects = discover_projects(config)
        for proj in projects:
            if not proj.project_dir:
                continue
            tasks = list_tasks(config, proj.id, state_filter=TaskState.BLOCKED)
            for task in tasks:
                blockers.append({"project": proj.id, "task": task.to_dict()})
        return blockers

    from pydantic import BaseModel

    class StateChangeRequest(BaseModel):
        state: str
        note: str | None = None

    @app.post("/api/tasks/{project_id}/{task_id}/state")
    def api_change_task_state(project_id: str, task_id: str, req: StateChangeRequest) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        from .models import TaskState
        try:
            state = TaskState(req.state)
            result = change_task_state(config, project_id, task_id, state, note=req.note)
            return {"success": True, "task": result.to_dict() if result else None}
        except Exception as e:
            return {"success": False, "error": str(e)}

    class RespondRequest(BaseModel):
        response: str
        unblock: bool = False

    @app.post("/api/tasks/{project_id}/{task_id}/respond")
    def api_respond_to_task(project_id: str, task_id: str, req: RespondRequest) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        try:
            from datetime import datetime
            from .models import TaskState

            from .tasks import get_tasks_dir
            from .concurrency import file_lock, retry_transient

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            response_line = f"\n{timestamp} [Web UI]: {req.response}"

            tasks_dir = get_tasks_dir(config, project_id)
            if not tasks_dir:
                return {"success": False, "error": "no_tasks_dir"}

            # CLAWP-066: append under the per-project lock with an atomic
            # tmp+replace, re-resolving the task inside the lock so the write
            # serialises against (and can't be clobbered by) a concurrent
            # change_task_state move.
            with file_lock(tasks_dir / ".clawpm-tasks.lock"):
                task = get_task(config, project_id, task_id)
                if not task or not task.file_path or not task.file_path.exists():
                    return {"success": False, "error": "task_not_found"}

                content = task.file_path.read_text(encoding="utf-8")

                # Add Responses section if not exists
                if "## Responses" not in content:
                    content += "\n\n## Responses\n"

                content += response_line
                tmp = task.file_path.with_suffix(".tmp")
                try:
                    tmp.write_text(content, encoding="utf-8")
                    retry_transient(tmp.replace, task.file_path)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise

            # Optionally unblock
            if req.unblock and task.state == TaskState.BLOCKED:
                change_task_state(config, project_id, task_id, TaskState.PROGRESS)

            return {"success": True, "timestamp": timestamp}
        except Exception as e:
            return {"success": False, "error": str(e)}

    class LogEntryRequest(BaseModel):
        action: str
        summary: str
        task: str | None = None
        next: str | None = None

    @app.post("/api/log")
    def api_add_log_entry(project_id: str, req: LogEntryRequest) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        from .models import WorkLogAction
        try:
            action = WorkLogAction(req.action)
            entry = add_entry(
                config,
                project=project_id,
                task=req.task,
                action=action,
                summary=req.summary,
                next=req.next,
            )
            return {"success": True, "entry": entry.to_dict() if entry else None}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/worklog")
    def api_worklog(project: str | None = None, limit: int = 10) -> list[dict]:
        config = load_portfolio_config()
        if not config:
            return []
        entries = tail_entries(config, project=project, limit=limit)
        return [e.to_dict() for e in entries]

    @app.get("/api/active-tasks")
    def api_active_tasks() -> list[dict]:
        config = load_portfolio_config()
        if not config:
            return []
        from .models import TaskState
        active = []
        projects = discover_projects(config)
        for proj in projects:
            if not proj.project_dir:
                continue
            for state in [TaskState.OPEN, TaskState.PROGRESS]:
                tasks = list_tasks(config, proj.id, state_filter=state)
                for task in tasks:
                    active.append({"project": proj.id, "project_name": proj.name, "task": task.to_dict()})
        return active

    @app.post("/api/projects/{project_id}/pause")
    def api_pause_project(project_id: str) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        try:
            project = get_project(config, project_id)
            if not project or not project.project_dir:
                return {"success": False, "error": "project_not_found"}

            settings_path = project.project_dir / ".project" / "settings.toml"
            content = settings_path.read_text(encoding="utf-8")
            content = content.replace('status = "active"', 'status = "paused"')
            settings_path.write_text(content, encoding="utf-8")

            return {"success": True, "status": "paused"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/projects/{project_id}/resume")
    def api_resume_project(project_id: str) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        try:
            project = get_project(config, project_id)
            if not project or not project.project_dir:
                return {"success": False, "error": "project_not_found"}

            settings_path = project.project_dir / ".project" / "settings.toml"
            content = settings_path.read_text(encoding="utf-8")
            content = content.replace('status = "paused"', 'status = "active"')
            settings_path.write_text(content, encoding="utf-8")

            return {"success": True, "status": "active"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    class CreateTaskRequest(BaseModel):
        project: str
        title: str
        priority: int = 3
        complexity: str = "m"
        description: str = ""

    @app.post("/api/tasks")
    def api_create_task(req: CreateTaskRequest) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        from .models import TaskComplexity
        from .tasks import add_task
        try:
            complexity = TaskComplexity(req.complexity) if req.complexity else TaskComplexity.M
            task = add_task(
                config,
                project_id=req.project,
                title=req.title,
                priority=req.priority,
                complexity=complexity,
                description=req.description,
            )
            if task:
                return {"success": True, "task": task.to_dict()}
            return {"success": False, "error": "failed_to_create_task"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    class CreateIssueRequest(BaseModel):
        project: str
        type: str = "bug"
        severity: str = "medium"
        command: str = ""
        expected: str = ""
        actual: str = ""
        context: str = ""

    @app.post("/api/issues")
    def api_create_issue(req: CreateIssueRequest) -> dict:
        config = load_portfolio_config()
        if not config:
            return {"error": "no_portfolio"}
        import json
        from datetime import datetime, timezone
        try:
            project = get_project(config, req.project)
            if not project or not project.project_dir:
                return {"success": False, "error": "project_not_found"}

            agent_dir = project.project_dir / ".agent"
            agent_dir.mkdir(exist_ok=True)
            issues_file = agent_dir / "issues.jsonl"

            entry = {
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "type": req.type,
                "severity": req.severity,
                "command": req.command or None,
                "expected": req.expected or None,
                "actual": req.actual or None,
                "context": req.context or None,
                "fixed": False,
            }
            entry = {k: v for k, v in entry.items() if v is not None}

            with open(issues_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return app
