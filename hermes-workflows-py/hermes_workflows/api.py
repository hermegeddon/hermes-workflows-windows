"""Dashboard contract, a dependency-free ASGI app, and an optional host router."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import settings
from .db import ConflictError, hermes_home
from .native import NativeUnavailable
from .redact import redact
from .runtime import Runtime, get_runtime
from .specs import compile_preview, validate
from .trace import read_trace

BASE = "/api/plugins/hermes-workflows"
MAX_BODY = 4 * 1024 * 1024
ROUTES = [
    ("GET", "/workflows"),
    ("POST", "/workflows"),
    ("GET", "/workflows/{workflow_id}"),
    ("PUT", "/workflows/{workflow_id}"),
    ("DELETE", "/workflows/{workflow_id}"),
    ("PUT", "/workflows/{workflow_id}/enabled"),
    ("GET", "/workflows/{workflow_id}/export"),
    ("GET", "/workflows/{workflow_id}/export-template"),
    ("POST", "/workflows/{workflow_id}/validate"),
    ("POST", "/workflows/{workflow_id}/compile-preview"),
    ("POST", "/workflows/{workflow_id}/run"),
    ("GET", "/runs"),
    ("GET", "/runs/{run_id}"),
    ("GET", "/runs/{run_id}/attempts"),
    ("GET", "/runs/{run_id}/notifications"),
    ("GET", "/runs/{run_id}/memory"),
    ("POST", "/runs/{run_id}/memory/{effect_id}/reconcile"),
    ("POST", "/runs/{run_id}/notifications/{notice_id}/reconcile"),
    ("GET", "/runs/{run_id}/export"),
    ("POST", "/runs/{run_id}/review"),
    ("POST", "/runs/{run_id}/cancel"),
    ("POST", "/runs/{run_id}/retry"),
    ("POST", "/runs/{run_id}/resume"),
    ("POST", "/runs/{run_id}/approvals/{request_id}"),
    ("GET", "/schedules"),
    ("PUT", "/schedules/{job_id}"),
    ("DELETE", "/schedules/{job_id}"),
    ("POST", "/schedules/{job_id}/pause"),
    ("POST", "/schedules/{job_id}/resume"),
    ("POST", "/schedules/{job_id}/run"),
    ("GET", "/settings"),
    ("PUT", "/settings"),
    ("GET", "/profiles"),
    ("GET", "/o2b-status"),
    ("GET", "/memory-status"),
]


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


class API:
    def __init__(self, runtime: Runtime | None = None, *, origin=None, origin_metadata=None):
        self._runtime = runtime
        self.origin = origin
        self.origin_metadata = origin_metadata

    @property
    def runtime(self):
        return self._runtime or get_runtime()

    def handle(self, method: str, target: str, payload: object = None) -> tuple[int, object]:
        try:
            return 200, self.dispatch(method.upper(), target, {} if payload is None else payload)
        except APIError as exc:
            return exc.status, {"detail": exc.detail}
        except KeyError as exc:
            return 404, {"detail": str(exc).strip("'")}
        except ConflictError as exc:
            return 409, {"detail": str(exc)}
        except (ValueError, TypeError) as exc:
            return 400, {"detail": str(exc)}
        except NativeUnavailable as exc:
            return 503, {"detail": str(exc)}
        except (sqlite3.Error, OSError):
            return 503, {"detail": "workflow storage is temporarily unavailable"}

    def dispatch(self, method: str, target: str, payload: object):
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        parts = urlsplit(target)
        path = parts.path.rstrip("/")
        if path == BASE or path.startswith(BASE + "/"):
            path = path[len(BASE) :]
        bits = [unquote(p) for p in path.split("/") if p]
        query = parse_qs(parts.query)
        runtime = self.runtime
        db, store, engine = runtime.db, runtime.specs, runtime.engine
        if path == "/settings":
            if method == "GET":
                return {
                    "values": settings.values(db),
                    "schema": settings.schema(db),
                    "memory": runtime.memory.status(),
                }
            if method == "PUT":
                return {
                    "values": settings.save(db, payload),
                    "schema": settings.schema(db),
                    "memory": runtime.memory.status(),
                }
        if path == "/memory-status" and method == "GET":
            return runtime.memory.status()
        if path == "/o2b-status" and method == "GET":
            installed = shutil.which("o2b") is not None
            return {
                "installed": installed,
                "connected": installed
                and (Path.home() / ".config" / "open-second-brain" / "config.yaml").is_file(),
            }
        if path == "/profiles" and method == "GET":
            names = {"default"}
            profiles = hermes_home() / "profiles"
            if profiles.exists():
                names.update(
                    p.name
                    for p in profiles.iterdir()
                    if p.is_dir() and (p / "config.yaml").exists()
                )
            roster = hermes_home() / "agent-roster" / "agents.yaml"
            if roster.exists():
                from .specs import parse_document

                agents = parse_document(roster.read_text(encoding="utf-8")).get("agents", {})
                if isinstance(agents, dict):
                    names.update(agents)
            return {"profiles": sorted(names)}
        if path == "/workflows":
            if method == "GET":
                latest = {}
                for row in db.list_runs("all"):
                    latest.setdefault(row["workflow_id"], row)
                schedules = {s["workflow_id"]: s for s in runtime.schedules.rows()}
                result = []
                for detail in store.all():
                    wf = detail["workflow"]
                    run = latest.get(wf["id"], {})
                    schedule = schedules.get(wf["id"], {})
                    result.append(
                        {
                            "id": wf["id"],
                            "name": wf.get("name", wf["id"]),
                            "scope": wf.get("scope", {}).get("type", "global"),
                            "trigger": wf.get("trigger", {"type": "manual"}),
                            "enabled": wf.get("enabled", True),
                            "last_run_at": run.get("started_at"),
                            "last_status": run.get("status"),
                            "next_run_at": schedule.get("next_run")
                            if schedule.get("enabled")
                            else None,
                        }
                    )
                return {"workflows": result}
            if method == "POST":
                detail = store.save(payload, create=True)
                runtime.sync_schedule(detail["workflow"])
                return detail
        if bits and bits[0] == "workflows" and len(bits) in (2, 3):
            workflow_id = bits[1]
            action = bits[2] if len(bits) == 3 else ""
            detail = store.get(workflow_id)
            wf = detail["workflow"]
            if not action:
                if method == "GET":
                    return detail
                if method == "PUT":
                    if (
                        not isinstance(payload.get("workflow"), dict)
                        or payload["workflow"].get("id") != workflow_id
                    ):
                        raise ValueError("workflow id in body does not match URL")
                    saved = store.save(payload)
                    runtime.sync_schedule(saved["workflow"])
                    return saved
                if method == "DELETE":
                    if db.list_runs("active", workflow_id):
                        raise ConflictError("cancel active runs before deleting the workflow")
                    runtime.schedules.delete_workflow(workflow_id)
                    store.delete(workflow_id)
                    return {"deleted": True}
            if action == "enabled" and method == "PUT":
                if type(payload.get("enabled")) is not bool:
                    raise ValueError("enabled must be boolean")
                wf["enabled"] = payload["enabled"]
                saved = store.save(detail)
                runtime.sync_schedule(wf)
                return saved
            if action == "validate" and method == "POST":
                return validate(wf)
            if action == "compile-preview" and method == "POST":
                plan = compile_preview(
                    wf,
                    mode=settings.values(db)["default_mode"],
                    project_id=payload.get("project_id"),
                )
                plan["native_memory"] = runtime.memory.preview(wf)
                return plan
            if action == "run" and method == "POST":
                state = engine.start(
                    wf,
                    workflow_path=detail["path"],
                    project_id=payload.get("project_id"),
                    input=payload.get("input"),
                    params=payload.get("params"),
                    origin=self.origin,
                    origin_metadata=self.origin_metadata,
                )
                runtime.start()
                return {"run_id": state["run_id"], "status": state["status"]}
            if action == "export" and method == "GET":
                return {
                    "id": workflow_id,
                    "filename": workflow_id + ".workflow.yaml",
                    "yaml": Path(detail["path"]).read_text(encoding="utf-8-sig"),
                }
            if action == "export-template" and method == "GET":
                from .template_export import export

                return export(wf, db)
        if path == "/runs" and method == "GET":
            scope = query.get("scope", ["active"])[0]
            if scope not in ("active", "all"):
                raise ValueError("scope must be active or all")
            return {"runs": db.list_runs(scope, query.get("workflow_id", [None])[0])}
        if (
            len(bits) == 5
            and bits[0] == "runs"
            and bits[2] == "memory"
            and bits[4] == "reconcile"
            and method == "POST"
        ):
            return asyncio.run(runtime.memory.reconcile(bits[1], bits[3]))
        if (
            len(bits) == 5
            and bits[0] == "runs"
            and bits[2] == "notifications"
            and bits[4] == "reconcile"
            and method == "POST"
        ):
            if not any(n["notice_id"] == bits[3] for n in runtime.notifications.records(bits[1])):
                raise KeyError("notification not found for this run")
            runtime.notifications.reconcile(bits[3], delivered=payload.get("delivered"))
            return {"notice_id": bits[3], "status": "sent" if payload["delivered"] else "failed"}
        if len(bits) == 4 and bits[0] == "runs" and bits[2] == "approvals" and method == "POST":
            for key in ("node_id", "attempt_id", "choice"):
                if not isinstance(payload.get(key), str) or not payload[key]:
                    raise ValueError(f"{key} is required")
            return runtime.telemetry.decide(
                bits[1], bits[3], payload["node_id"], payload["attempt_id"], payload["choice"]
            )
        if bits and bits[0] == "runs" and len(bits) in (2, 3):
            run_id, action = bits[1], bits[2] if len(bits) == 3 else ""
            state = db.get_run(run_id)
            if action == "attempts" and method == "GET":
                return {"attempts": redact(db.attempts(run_id))}
            if action == "notifications" and method == "GET":
                return {"notifications": runtime.notifications.records(run_id)}
            if action == "memory" and method == "GET":
                return {
                    "memory": state.get("memory_export"),
                    "effects": runtime.memory.records(run_id),
                }
            if not action and method == "GET":
                # Use this run's immutable spec, rather than a subsequently
                # edited workflow, to describe the gate's allowed decisions.
                for node in db.get_spec(run_id)["nodes"]:
                    if node["type"] == "human_review":
                        state["nodes"][node["id"]]["review_options"] = node.get(
                            "options", ["approved", "rejected", "needs_changes"]
                        )
                return runtime.telemetry.overlay(state)
            if action == "export" and method == "GET":
                result = {
                    "run_id": run_id,
                    "filename": run_id + ".run.json",
                    "json": redact(runtime.telemetry.overlay(state)),
                }
                result["json"]["attempts"] = redact(db.attempts(run_id))
                trace = read_trace(db, run_id)
                if trace is not None:
                    result.update(trace=trace, trace_filename=run_id + ".trace.jsonl")
                return result
            if action == "cancel" and method == "POST":
                return engine.cancel(run_id)
            if action == "review" and method == "POST":
                if not isinstance(payload.get("node_id"), str) or not isinstance(
                    payload.get("decision"), str
                ):
                    raise ValueError("node_id and decision are required strings")
                result = engine.review(
                    run_id,
                    payload["node_id"],
                    payload["decision"],
                    payload.get("note"),
                    attempt_id=payload.get("attempt_id"),
                )
                runtime.start()
                return result
            if action == "retry" and method == "POST":
                if "node_id" in payload and (
                    not isinstance(payload["node_id"], str) or not payload["node_id"].strip()
                ):
                    raise ValueError("node_id must be a non-empty string")
                workflow = store.get(state["workflow_id"])["workflow"]
                result = engine.retry(run_id, workflow, payload.get("node_id"))
                runtime.start()
                return result
            if action == "resume" and method == "POST":
                if set(payload) - {"node", "node_id", "all"}:
                    raise ValueError("unknown resume option")
                if "node" in payload and "node_id" in payload:
                    raise ValueError("node and node_id are mutually exclusive")
                result = engine.resume(
                    run_id,
                    store.get(state["workflow_id"])["workflow"],
                    node=payload.get("node", payload.get("node_id")),
                    reset_all=payload.get("all", False),
                )
                runtime.start()
                return result
        if path == "/schedules" and method == "GET":
            return {"schedules": runtime.schedules.rows()}
        if bits and bits[0] == "schedules" and len(bits) in (2, 3):
            action = bits[2] if len(bits) == 3 else ""
            if not action and method == "DELETE":
                return runtime.schedules.action(bits[1], "delete")
            if not action and method == "PUT":
                return runtime.schedules.action(bits[1], "edit", payload.get("cron"))
            if method == "POST" and action in ("pause", "resume", "run"):
                return runtime.schedules.action(bits[1], action)
        matching = [
            m for m, route in ROUTES if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", route), path)
        ]
        raise APIError(
            405 if matching else 404, "method not allowed" if matching else "route not found"
        )


def decode_body(raw: bytes) -> object:
    if len(raw) > MAX_BODY:
        raise APIError(413, "request body too large")
    try:

        def reject_constant(value):
            raise ValueError(f"non-finite JSON number: {value}")

        return json.loads(raw, parse_constant=reject_constant) if raw else {}
    except (ValueError, UnicodeError) as exc:
        raise APIError(400, "invalid JSON body") from exc


class ASGIApp:
    def __init__(self, api: API | None = None):
        self.api = api or API()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            from .runtime import own_runtime

            release = None
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    release = own_runtime(await asyncio.to_thread(lambda: self.api.runtime))
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if release:
                        await asyncio.to_thread(release)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        body = bytearray()
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY:
                    raise APIError(413, "request body too large")
                if not message.get("more_body"):
                    break
            target = scope["path"] + (
                "?" + scope.get("query_string", b"").decode() if scope.get("query_string") else ""
            )
            status, data = await asyncio.to_thread(
                self.api.handle, scope["method"], target, decode_body(bytes(body))
            )
        except APIError as exc:
            status, data = exc.status, {"detail": exc.detail}
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})


app = ASGIApp()


def create_router(api: API | None = None):
    """FastAPI belongs to the Hermes dashboard; the engine needs only stdlib."""
    from contextlib import asynccontextmanager

    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    from .runtime import own_runtime

    api = api or API()

    @asynccontextmanager
    async def lifespan(app):
        runtime = await asyncio.to_thread(lambda: api.runtime)
        release = own_runtime(runtime)
        try:
            yield
        finally:
            await asyncio.to_thread(release)

    router = APIRouter(lifespan=lifespan)

    async def endpoint(request):
        try:
            raw = await request.body()
            target = request.url.path + ("?" + request.url.query if request.url.query else "")
            status, data = await asyncio.to_thread(
                api.handle, request.method, target, decode_body(raw)
            )
        except APIError as exc:
            status, data = exc.status, {"detail": exc.detail}
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    endpoint.__annotations__["request"] = Request
    for method, path in ROUTES:
        router.add_api_route(path, endpoint, methods=[method])
    return router
