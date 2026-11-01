"""One schedule authority per row; native cron launches a Python-only trigger.

SQLite serializes handoffs and reconciles a paused native job after a crash.
Native rows have no local due time, including for older Python supervisors.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import settings
from .db import ConflictError, Database
from .native import NativeHost, NativeUnavailable
from .schedules import Cron, iso_now


class Schedules:
    def __init__(self, runtime, *, host=None):
        self.runtime = runtime
        self.db, self.home = runtime.db, runtime.home
        self.host = host or NativeHost(self.home)

    @contextmanager
    def jobs(self):
        with self.host.scope():
            jobs = self.host.module("cron")
            with jobs.use_cron_store(self.home):
                yield jobs

    def native_available(self):
        try:
            with self.jobs():
                return True
        except NativeUnavailable:
            return False

    def script(self, workflow_id):
        identity = hashlib.sha256(
            (str(self.db.path.resolve()) + "\0" + workflow_id).encode()
        ).hexdigest()[:24]
        return self.home / "scripts" / ("hermes-workflows-" + identity + ".py")

    def write_script(self, workflow_id, *, hermes_source=None):
        path = self.script(workflow_id)
        package = Path(__file__).resolve().parent.parent
        body = (
            "# Generated Hermes Workflows trigger. No shell or provider bootstrap.\n"
            "import sys\n"
            f"sys.path.insert(0, {str(package)!r})\n"
            + (f"sys.path.insert(0, {str(hermes_source)!r})\n" if hermes_source else "")
            + "from hermes_workflows.native_schedules import fire\n"
            f"fire({str(self.db.path.resolve())!r}, {workflow_id!r}, hermes_source={str(hermes_source) if hermes_source else None!r})\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(body, encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def owned(self, jobs, row):
        job = jobs.get_job(row["native_id"]) if row.get("native_id") else None
        if (
            job is not None
            and Path(job.get("script") or "").resolve() != self.script(row["workflow_id"]).resolve()
        ):
            raise ConflictError("native schedule script was replaced; reconcile it before editing")
        return job

    @staticmethod
    def store(con, row):
        con.execute(
            "INSERT INTO py_schedules VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (row["hermes_cron_id"], row["workflow_id"], json.dumps(row)),
        )

    def sync(self, workflow):
        mode = settings.values(self.db)["scheduler_backend"]
        native = mode != "standalone" and self.native_available()
        if mode == "native" and not native:
            raise NativeUnavailable("installed Hermes lacks profile-scoped cron APIs")
        key = "workflow-" + workflow["id"]
        with self.db.connect(write=True) as con:
            saved = con.execute("SELECT data FROM py_schedules WHERE id=?", (key,)).fetchone()
            old = json.loads(saved[0]) if saved else None
            trigger = workflow["trigger"]
            if trigger["type"] != "cron":
                if old:
                    self.remove_native(old)
                    con.execute("DELETE FROM py_schedules WHERE id=?", (key,))
                return
            signature = json.dumps(trigger, sort_keys=True)
            changed = old is None or old.get("spec_signature") != signature
            toggled = old is not None and old.get("workflow_enabled") != workflow.get(
                "enabled", True
            )
            row = dict(
                old or {}, hermes_cron_id=key, workflow_id=workflow["id"], spec_signature=signature
            )
            if changed:
                if (old or {}).get("deleted"):
                    row.pop("native_id", None)
                cadence = Cron(trigger["schedule"], trigger.get("timezone", "UTC"))
                row.update(
                    cron_expression=trigger["schedule"],
                    timezone=trigger.get("timezone", "UTC"),
                    enabled=workflow.get("enabled", True),
                    deleted=False,
                    next_run=cadence.next().isoformat(),
                    last_run=(old or {}).get("last_run"),
                )
            elif toggled:
                row["enabled"] = workflow.get("enabled", True)
            row["workflow_enabled"] = workflow.get("enabled", True)
            if row.get("deleted"):
                self.store(con, row)
                return
            if native:
                self.to_native(con, row, changed=changed or toggled)
            else:
                if row.get("authority") == "native":
                    # Absence never silently reactivates Python firing. An
                    # explicit standalone choice must first pause its native job.
                    if mode != "standalone":
                        raise NativeUnavailable("native schedule authority is unavailable")
                    with self.jobs() as jobs:
                        job = self.owned(jobs, row)
                        if job:
                            if not changed and not toggled:
                                row.update(self.project_job(job))
                            jobs.pause_job(job["id"])
                    row["generation"] = uuid.uuid4().hex
                    row["next_run"] = (
                        row.get("next_run")
                        or Cron(row["cron_expression"], row["timezone"]).next().isoformat()
                    )
                row["authority"] = "standalone"
                self.store(con, row)

    def to_native(self, con, row, *, changed):
        with self.jobs() as jobs:
            source = getattr(jobs, "__file__", None)
            script = self.write_script(
                row["workflow_id"],
                hermes_source=Path(source).resolve().parent.parent if source else None,
            )
            job = self.owned(jobs, row)
            if row.get("native_id") and job is None:
                # Deletion from native host views is respected across restart.
                row.update(deleted=True, enabled=False, next_run=None)
                self.store(con, row)
                return
            migrating = row.get("authority") != "native"
            if job is None:
                for candidate in jobs.list_jobs(include_disabled=True):
                    if Path(candidate.get("script") or "").resolve() == script.resolve():
                        if job is not None:
                            raise ConflictError(
                                "duplicate native workflow jobs require reconciliation"
                            )
                        job = candidate
                    elif candidate.get("name") == "workflow:" + row["workflow_id"]:
                        raise ConflictError("workflow schedule name belongs to another script")
                if job is None:
                    job = jobs.create_job(
                        prompt=None,
                        schedule=row["cron_expression"],
                        timezone=row["timezone"],
                        name="workflow:" + row["workflow_id"],
                        script=str(script),
                        no_agent=True,
                        deliver="local",
                        paused=True,
                    )
            if migrating or changed:
                jobs.pause_job(job["id"])
                jobs.update_job(
                    job["id"], {"schedule": row["cron_expression"], "timezone": row["timezone"]}
                )
                active = row["enabled"] and row["workflow_enabled"]
                due = row.get("next_run") or jobs.get_job(job["id"]).get("next_run_at")
                row.update(
                    authority="native",
                    native_id=job["id"],
                    next_run=None,
                    generation=uuid.uuid4().hex,
                )
                self.store(con, row)
                # Publish no local due time before enabling the native side.
                # Its callback waits on this transaction and rechecks authority.
                jobs.update_job(
                    job["id"],
                    {
                        "enabled": active,
                        "state": "scheduled" if active else "paused",
                        "paused_at": None if active else iso_now(),
                        "paused_reason": None if active else "Workflow disabled or paused",
                        "next_run_at": due,
                    },
                )
            else:
                row.update(authority="native", native_id=job["id"], next_run=None)
                self.store(con, row)

    @staticmethod
    def project_job(job):
        schedule = job.get("schedule") or {}
        return {
            "cron_expression": schedule.get("expr") or job.get("schedule_display"),
            "timezone": job.get("timezone") or "UTC",
            "enabled": bool(job.get("enabled")),
            "last_run": job.get("last_run_at"),
            "next_run": job.get("next_run_at"),
        }

    def rows(self):
        result = []
        for saved in self.db.schedules():
            if saved.get("deleted"):
                continue
            row = {
                k: saved[k]
                for k in (
                    "hermes_cron_id",
                    "workflow_id",
                    "cron_expression",
                    "timezone",
                    "enabled",
                    "last_run",
                    "next_run",
                )
            }
            if saved.get("authority") == "native":
                with self.jobs() as jobs:
                    job = self.owned(jobs, saved)
                    if job is None:
                        continue
                    row.update(self.project_job(job), hermes_cron_id=job["id"])
                    row["last_run"] = row["last_run"] or saved.get("last_run")
            result.append(row)
        return result

    def resolve(self, identity):
        for row in self.db.schedules():
            if identity in (row["hermes_cron_id"], row.get("native_id")) and not row.get("deleted"):
                return row
        raise KeyError("schedule not found")

    def remove_native(self, row):
        if row.get("native_id"):
            with self.jobs() as jobs:
                job = self.owned(jobs, row)
                if job:
                    jobs.pause_job(job["id"])
                    jobs.remove_job(job["id"])

    def delete_workflow(self, workflow_id):
        with self.db.connect(write=True) as con:
            found = con.execute(
                "SELECT data FROM py_schedules WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if found:
                row = json.loads(found[0])
                self.remove_native(row)
                con.execute("DELETE FROM py_schedules WHERE workflow_id=?", (workflow_id,))

    def action(self, identity, action, expression=None):
        with self.db.connect(write=True) as con:
            # Resolve again under the writer lock; another host may have handed
            # authority to/from native while this request was in flight.
            found = con.execute(
                "SELECT data FROM py_schedules WHERE id=? OR json_extract(data,'$.native_id')=?",
                (identity, identity),
            ).fetchone()
            if found is None or (row := json.loads(found[0])).get("deleted"):
                raise KeyError("schedule not found")
            if action == "edit":
                Cron(expression, row["timezone"])
            if row.get("authority") == "native":
                with self.jobs() as jobs:
                    job = self.owned(jobs, row)
                    if job is None:
                        raise KeyError("native schedule not found")
                    if action == "edit":
                        jobs.update_job(job["id"], {"schedule": expression})
                    elif action == "delete":
                        jobs.pause_job(job["id"])
                        jobs.remove_job(job["id"])
                    else:
                        getattr(
                            jobs,
                            {"pause": "pause_job", "resume": "resume_job", "run": "trigger_job"}[
                                action
                            ],
                        )(job["id"])
            if action == "delete":
                row.update(deleted=True, enabled=False, next_run=None)
            elif action == "edit":
                row.update(cron_expression=expression)
                if row.get("authority") != "native":
                    row["next_run"] = Cron(expression, row["timezone"]).next().isoformat()
            elif action in ("pause", "resume"):
                row["enabled"] = action == "resume"
                if action == "resume" and row.get("authority") != "native":
                    row["next_run"] = (
                        Cron(row["cron_expression"], row["timezone"]).next().isoformat()
                    )
            elif action == "run" and row.get("authority") == "native":
                row["enabled"] = True
            row["generation"] = uuid.uuid4().hex
            self.store(con, row)
        if action == "run" and row.get("authority") != "native":
            detail = self.runtime.specs.get(row["workflow_id"])

            def guard(con):
                saved = con.execute(
                    "SELECT data FROM py_schedules WHERE id=?", (row["hermes_cron_id"],)
                ).fetchone()
                live = json.loads(saved[0]) if saved else {}
                if (
                    live.get("authority") != "standalone"
                    or live.get("generation") != row.get("generation")
                    or live.get("deleted")
                ):
                    raise ConflictError("schedule authority changed before run-now")
                live["last_run"] = iso_now()
                self.store(con, live)

            started = self.runtime.engine.start(
                detail["workflow"], workflow_path=detail["path"], _schedule_guard=guard
            )
            self.runtime.start()
            return {"ok": True, "run_id": started["run_id"]}
        return {"deleted": True} if action == "delete" else {"ok": True}


def fire(path, workflow_id, *, hermes_source=None):
    """Native script entry: enqueue only under the current claimed job identity.

    Gateway supervisors drive the run. Retrying one native claim reuses its run
    ID even after settlement; a busy workflow coalesces instead of duplicating.
    """
    from types import SimpleNamespace

    from .engine import Engine
    from .executor import Executor
    from .specs import SpecStore

    if hermes_source:
        # A native Windows script may use the base store interpreter. Activate
        # only its already committed dependencies, never the updater/installer.
        # Borrowed temporary homes do not own the checkout's dependency store.
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from pm.environments import activate_dependencies, owning_home_root

        source = Path(hermes_source)
        owner = owning_home_root(source)
        token = set_hermes_home_override(owner) if owner else None
        try:
            activate_dependencies(source)
        finally:
            if token is not None:
                reset_hermes_home_override(token)
    db = Database(path)
    home = db.path.parent.parent.resolve()
    engine = Engine(db, Executor())
    engine.executor.home = home
    schedules = Schedules(SimpleNamespace(db=db, home=home))
    row = schedules.resolve("workflow-" + workflow_id)
    if row.get("authority") != "native":
        raise ConflictError("schedule is no longer native")
    with schedules.jobs() as jobs:
        job = schedules.owned(jobs, row)
        claim = (job or {}).get("fire_claim") or {}
        if not job or not job.get("enabled") or not claim.get("by"):
            raise ConflictError("native schedule has no active fire claim")
    identity = hashlib.sha256((job["id"] + claim["by"]).encode()).hexdigest()[:20]
    run_id = workflow_id + "-scheduled-" + identity

    def guard(con):
        latest = con.execute(
            "SELECT data FROM py_schedules WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        live = json.loads(latest[0]) if latest else {}
        if (
            live.get("authority") != "native"
            or live.get("generation") != row.get("generation")
            or live.get("deleted")
            or not live.get("workflow_enabled")
        ):
            raise ConflictError("schedule authority changed")
        live["last_run"] = iso_now()
        schedules.store(con, live)

    detail = SpecStore([home / "workflows/global", home / "workflows/templates"]).get(workflow_id)
    if json.dumps(detail["workflow"]["trigger"], sort_keys=True) != row["spec_signature"]:
        raise ConflictError("workflow trigger changed before schedule reconciliation")
    try:
        state = engine.start(
            detail["workflow"], workflow_path=detail["path"], run_id=run_id, _schedule_guard=guard
        )
        print(json.dumps({"run_id": state["run_id"]}))
    except ConflictError:
        print(json.dumps({"skipped": True, "workflow_id": workflow_id}))
