"""Attempt-scoped worker observations and human decisions, shared across hosts.

Worker connections are short and bounded. An observation failure loses optional
telemetry; it must never fail a provider/tool call. Approval failures deny.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .db import ACTIVE, ConflictError, Database
from .redact import redact
from .trace import safe_emit

COUNTERS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "api_calls",
    "tool_calls",
    "tool_errors",
    "subagents",
)


class TelemetryStore:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def connect(self, *, write=False):
        # Do not create or migrate a database from a worker or poll overlay.
        con = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=0.2, isolation_level=None
        )
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def current(self, con, context, *, observation=False):
        row = Database._owned_row(
            con,
            context["run_id"],
            revision=context["revision"],
            attempt=(context["node_id"], context["attempt_id"]),
        )
        run = json.loads(row["state"])
        if (
            run["status"] not in ACTIVE
            or run["nodes"][context["node_id"]]["status"] not in ("running", "scheduled")
            or Path(run.get("notification_context", {}).get("home", "")).resolve()
            != self.path.parent.parent
        ):
            raise ConflictError("worker attempt is no longer active")
        if context.get("native_db"):
            with closing(
                sqlite3.connect(
                    Path(context["native_db"]).as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
            ) as native:
                native.execute("BEGIN")
                task = native.execute(
                    "SELECT status,current_run_id,claim_lock FROM tasks WHERE id=?",
                    (context["stream_key"],),
                ).fetchone()
                active = task and tuple(task) == (
                    "running",
                    context["native_run_id"],
                    context["native_claim"],
                )
                finished = False
                if (
                    observation
                    and task
                    and tuple(task) in (("done", None, None), ("review", None, None))
                ):
                    # Completion releases the claim before post_tool_call.
                    # Only observations may finish that same bound stream;
                    # registration and human decisions still require a claim.
                    latest = native.execute(
                        "SELECT id,status,outcome,claim_lock,ended_at FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (context["stream_key"],),
                    ).fetchone()
                    finished = bool(
                        latest
                        and latest[0] == context["native_run_id"]
                        and (latest[1], latest[2])
                        == (task[0], "completed" if task[0] == "done" else "review_requested")
                        and latest[3] == context["native_claim"]
                        and latest[4] is not None
                    )
                if not active and not finished:
                    raise ConflictError("native worker claim is no longer active")
                bound = native.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
                    (context["stream_key"], context["native_run_id"]),
                ).fetchone()
                if (finished and not bound) or (
                    bound and json.loads(bound[0]).get("pid") != context["native_pid"]
                ):
                    raise ConflictError("native worker process was replaced")
        return run

    def register(self, context):
        identity, now = uuid.uuid4().hex, time.time()
        with self.connect(write=True) as con:
            self.current(con, context)
            previous = con.execute(
                "SELECT context FROM py_workers WHERE attempt_id=? AND stream_key=? AND active=1",
                (context["attempt_id"], context["stream_key"]),
            ).fetchone()
            if previous and json.loads(previous[0]).get("native_run_id", 0) > context.get(
                "native_run_id", 0
            ):
                raise ConflictError("native worker was superseded")
            if (
                previous
                and context.get("native_db")
                and json.loads(previous[0]).get("native_run_id") == context["native_run_id"]
                and json.loads(previous[0]).get("native_pid") == context["native_pid"]
            ):
                raise ConflictError("native worker is already observed")
            con.execute(
                "UPDATE py_workers SET active=0 WHERE attempt_id=? AND stream_key=?",
                (context["attempt_id"], context["stream_key"]),
            )
            con.execute(
                "INSERT INTO py_workers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    context["run_id"],
                    context["node_id"],
                    context["attempt_id"],
                    context["revision"],
                    context["stream_key"],
                    1,
                    json.dumps(context),
                    "{}",
                    0,
                    now,
                    now,
                ),
            )
        return identity

    def worker(self, con, identity, *, observation=False):
        row = con.execute("SELECT * FROM py_workers WHERE worker_id=?", (identity,)).fetchone()
        if row is None or not row["active"]:
            raise ConflictError("worker was superseded")
        context = json.loads(row["context"])
        run = self.current(con, context, observation=observation)
        return row, context, run

    def write(self, identity, data, sequence):
        with self.connect(write=True) as con:
            _, context, run = self.worker(con, identity, observation=True)
            updated = con.execute(
                "UPDATE py_workers SET data=?,sequence=?,updated_at=? WHERE worker_id=? AND sequence<?",
                (
                    json.dumps(redact(data), ensure_ascii=False, allow_nan=False),
                    sequence,
                    time.time(),
                    identity,
                    sequence,
                ),
            )
            if updated.rowcount:
                safe_emit(
                    con,
                    run,
                    "worker_telemetry",
                    node_id=context["node_id"],
                    attempt_id=context["attempt_id"],
                    worker_id=identity,
                    sequence=sequence,
                    telemetry=redact(data),
                )

    def aggregate(self, attempt_id, *, connection=None):
        try:

            def read(con):
                rows = con.execute(
                    "SELECT worker_id,data,updated_at FROM py_workers WHERE attempt_id=? AND active=1 ORDER BY updated_at",
                    (attempt_id,),
                ).fetchall()
                requests = con.execute(
                    "SELECT a.*,w.worker_id FROM py_worker_approvals a JOIN py_workers w USING(worker_id) WHERE w.attempt_id=? AND w.active=1 ORDER BY a.created_at",
                    (attempt_id,),
                ).fetchall()
                return rows, requests

            if connection is None:
                with self.connect() as con:
                    rows, requests = read(con)
            else:
                rows, requests = read(connection)
            result, approvals = {}, []
            controlled = {r["worker_id"]: r["created_at"] for r in requests}
            readonly = set()
            for row in rows:
                data = json.loads(row["data"])
                for key in COUNTERS:
                    if key in data:
                        result[key] = result.get(key, 0) + data[key]
                if "duration_ms" in data:
                    result["duration_ms"] = max(result.get("duration_ms", 0), data["duration_ms"])
                for key in ("error_type", "error_message"):
                    if key in data:
                        result[key] = data[key]
                if data.get("approval") and (
                    row["worker_id"] not in controlled
                    or data["approval"].get("requested_at", 0) > controlled[row["worker_id"]]
                ):
                    approvals.append(data["approval"])
                    readonly.add(row["worker_id"])
            # Native callback rows are authoritative even when another observer
            # event flushes a recorder's older in-memory aggregate.
            latest = {}
            pending = []
            waiting_workers = set()
            for request in requests:
                payload = json.loads(request["payload"])
                payload["can_respond"] = (
                    request["status"] == "pending" and request["expires_at"] > time.time()
                )
                payload["state"] = (
                    "pending"
                    if request["status"] in ("pending", "answered")
                    and request["expires_at"] > time.time()
                    else "resolved"
                )
                if request["choice"]:
                    payload["choice"] = request["choice"]
                elif request["expires_at"] <= time.time():
                    payload["choice"] = "timeout"
                if payload["state"] == "resolved":
                    payload["resolved_at"] = request["updated_at"]
                latest[request["worker_id"]] = payload
                if payload["state"] == "pending":
                    pending.append(payload)
                    waiting_workers.add(request["worker_id"])
            approvals.extend(pending)
            approvals.extend(
                value for key, value in latest.items() if key not in waiting_workers | readonly
            )
            if approvals:
                result["approval"] = next(
                    (a for a in approvals if a["state"] == "pending"), approvals[-1]
                )
                result["approvals"] = approvals
            return redact(result)
        except Exception:
            return {}

    def overlay(self, run):
        if run["status"] not in ACTIVE:
            return run
        for node in run["nodes"].values():
            if node.get("attempt_id") and node["status"] in ("running", "scheduled"):
                data = self.aggregate(node["attempt_id"])
                if data:
                    node["telemetry"] = {**node.get("telemetry", {}), **data}
        return run

    def decide(self, run_id, request_id, node_id, attempt_id, choice):
        if choice not in ("once", "deny"):
            raise ValueError("choice must be once or deny")
        with self.connect(write=True) as con:
            request = con.execute(
                "SELECT * FROM py_worker_approvals WHERE request_id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise KeyError("command approval not found")
            worker, context, run = self.worker(con, request["worker_id"])
            if (context["run_id"], context["node_id"], context["attempt_id"]) != (
                run_id,
                node_id,
                attempt_id,
            ):
                raise ConflictError("command approval does not belong to this attempt")
            if request["expires_at"] <= time.time() or request["status"] != "pending":
                if request["status"] in ("answered", "resolved") and request["choice"] == choice:
                    return {"request_id": request_id, "status": request["status"]}
                raise ConflictError("command approval is no longer pending")
            if choice not in json.loads(request["payload"])["choices"]:
                raise ValueError("choice is not allowed by the native request")
            con.execute(
                "UPDATE py_worker_approvals SET status='answered',choice=?,updated_at=? WHERE request_id=?",
                (choice, time.time(), request_id),
            )
            safe_emit(
                con,
                run,
                "approval_decided",
                node_id=node_id,
                attempt_id=attempt_id,
                request_id=request_id,
                choice=choice,
            )
        return {"request_id": request_id, "status": "answered"}

    def request(self, identity, command, description, *, timeout=300, native_request=None):
        if not math.isfinite(timeout) or timeout <= 0:
            return "deny"
        request_id, token, now = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
        choices = [
            c
            for c in ("once", "deny")
            if native_request is None or c in native_request.allowed_choices
        ]
        payload = redact(
            {
                "state": "pending",
                "request_id": request_id,
                "can_respond": True,
                "command": command,
                "description": description,
                "surface": "workflow-dashboard",
                "requested_at": now,
                "choices": choices,
            }
        )
        with self.connect(write=True) as con:
            worker, context, run = self.worker(con, identity)
            con.execute(
                "INSERT INTO py_worker_approvals VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    identity,
                    token,
                    json.dumps(payload),
                    "pending",
                    None,
                    now + timeout,
                    now,
                    now,
                ),
            )
            data = json.loads(worker["data"])
            data["approval"] = payload
            con.execute(
                "UPDATE py_workers SET data=?,updated_at=? WHERE worker_id=?",
                (json.dumps(data), now, identity),
            )
            safe_emit(
                con,
                run,
                "approval_requested",
                node_id=context["node_id"],
                attempt_id=context["attempt_id"],
                request_id=request_id,
                approval=payload,
            )
        choice = "timeout"
        try:
            deadline = time.monotonic() + max(0, timeout)
            while time.monotonic() < deadline:
                with self.connect() as con:
                    self.worker(con, identity)
                    request = con.execute(
                        "SELECT * FROM py_worker_approvals WHERE request_id=? AND token=?",
                        (request_id, token),
                    ).fetchone()
                    if request["status"] == "answered":
                        choice = request["choice"]
                        break
                    if request["status"] != "pending":
                        choice = "cancelled"
                        break
                time.sleep(0.1)
        except Exception:
            choice = "cancelled"
        finally:
            try:
                with self.connect(write=True) as con:
                    # Recheck ownership under the writer lock. Cancellation or
                    # a retry can revoke even an already answered prompt.
                    try:
                        _, context, run = self.worker(con, identity)
                        request = con.execute(
                            "SELECT * FROM py_worker_approvals WHERE request_id=? AND token=?",
                            (request_id, token),
                        ).fetchone()
                        if (
                            request["status"] == "answered"
                            and request["updated_at"] < request["expires_at"]
                            and time.monotonic() < deadline
                        ):
                            choice = request["choice"]
                        elif choice == "once":
                            choice = "timeout"
                    except Exception:
                        choice = "cancelled"
                        run = None
                    con.execute(
                        "UPDATE py_worker_approvals SET status='resolved',choice=?,updated_at=? WHERE request_id=? AND token=?",
                        (choice, time.time(), request_id, token),
                    )
                    if run is not None:
                        safe_emit(
                            con,
                            run,
                            "approval_resolved",
                            node_id=context["node_id"],
                            attempt_id=context["attempt_id"],
                            request_id=request_id,
                            choice=choice,
                        )
                    row = con.execute(
                        "SELECT data FROM py_workers WHERE worker_id=?", (identity,)
                    ).fetchone()
                    data = json.loads(row[0])
                    # Never close a newer concurrent prompt in this worker.
                    if data.get("approval", {}).get("request_id") == request_id:
                        data["approval"].update(
                            state="resolved",
                            choice=choice,
                            resolved_at=time.time(),
                            can_respond=False,
                        )
                        con.execute(
                            "UPDATE py_workers SET data=?,updated_at=? WHERE worker_id=?",
                            (json.dumps(data), time.time(), identity),
                        )
            except Exception:
                choice = "cancelled"
        return choice if choice in choices else "deny"


class NodeTelemetryRecorder:
    def __init__(self, store, context, *, now=time.time):
        self.store, self.context, self.now = store, context, now
        self.identity = store.register(context)
        self.data, self.sequence, self.first = {}, 0, None
        self.lock, self.seen = threading.RLock(), set()

    def record(self, kind, **fields):
        try:
            with self.lock:
                key = (
                    fields.get("api_request_id")
                    if kind in ("api", "api_error")
                    else fields.get("tool_call_id")
                    if kind == "tool"
                    else None
                )
                if key and (kind, key) in self.seen:
                    return
                if key:
                    self.seen.add((kind, key))
                bump = (
                    "api_calls"
                    if kind in ("api", "api_error")
                    else "tool_calls"
                    if kind == "tool"
                    else "subagents"
                    if kind == "subagent"
                    else None
                )
                if bump:
                    self.data[bump] = self.data.get(bump, 0) + 1
                if kind == "api":
                    usage = fields.get("usage") or {}
                    usage = usage if isinstance(usage, dict) else {}
                    for target, source in (
                        ("input_tokens", "prompt_tokens"),
                        ("output_tokens", "completion_tokens"),
                    ):
                        value = usage.get(target, usage.get(source))
                        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                            self.data[target] = self.data.get(target, 0) + int(value)
                    if "input_tokens" in self.data or "output_tokens" in self.data:
                        self.data["total_tokens"] = self.data.get(
                            "input_tokens", 0
                        ) + self.data.get("output_tokens", 0)
                if kind == "tool" and (fields.get("status") == "error" or fields.get("error_type")):
                    self.data["tool_errors"] = self.data.get("tool_errors", 0) + 1
                error = fields.get("error") if isinstance(fields.get("error"), dict) else fields
                if (
                    kind == "api_error"
                    or kind == "tool"
                    and (fields.get("error_type") or fields.get("status") == "error")
                ):
                    for target, key in (("error_type", "type"), ("error_message", "message")):
                        value = error.get(target, error.get(key))
                        if value:
                            self.data[target] = redact(str(value))[:4096]
                if kind == "approval_request":
                    self.data["approval"] = {
                        "state": "pending",
                        "requested_at": self.now(),
                        "can_respond": False,
                        **{
                            k: redact(str(fields[k]))[:4096]
                            for k in (
                                "command",
                                "description",
                                "surface",
                                "session_key",
                                "request_id",
                            )
                            if fields.get(k)
                        },
                    }
                elif kind == "approval_response":
                    self.data.setdefault("approval", {}).update(
                        state="resolved",
                        choice=str(fields.get("choice") or "cancelled"),
                        resolved_at=self.now(),
                        can_respond=False,
                    )
                elif kind != "approval_request":
                    ts = self.now()
                    self.first = ts if self.first is None else self.first
                    self.data["duration_ms"] = max(0, round((ts - self.first) * 1000))
                self.sequence += 1
                self.store.write(self.identity, self.data, self.sequence)
        except Exception:
            pass

    def approval(self, command, description, **kwargs):
        try:
            return self.store.request(self.identity, command, description, **kwargs)
        except Exception:
            return "deny"
