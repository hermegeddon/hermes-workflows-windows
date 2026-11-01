"""Transactional JSONL timeline. SQLite is canonical across embedded hosts."""

import json
import sqlite3
import time

from .redact import redact


def emit(con, run, kind, **payload):
    if not run.get("observability", {}).get("trace_enabled"):
        return
    event = {
        "ts": time.time(),
        "run_id": run["run_id"],
        "kind": kind,
        "revision": run.get("execution_revision", 0),
        **{k: v for k, v in payload.items() if v is not None},
    }
    con.execute(
        "INSERT INTO py_traces(run_id,event) VALUES(?,?)",
        (run["run_id"], json.dumps(redact(event), ensure_ascii=False)),
    )


def transitions(con, before, run, event=None):
    if not run.get("observability", {}).get("trace_enabled"):
        return
    if before is None:
        emit(con, run, "run_started", workflow_id=run["workflow_id"])
        return
    for node_id, node in run["nodes"].items():
        old = before["nodes"].get(node_id, {})
        if old.get("status") == node["status"] and old.get("attempt_id") == node.get("attempt_id"):
            continue
        fields = {
            "node_id": node_id,
            "attempt_id": node.get("attempt_id"),
            "iteration": node.get("iteration", 0),
            "attempt": node.get("attempt", 0),
        }
        if node["status"] == "completed" and old.get("status") in ("running", "scheduled"):
            emit(
                con, run, "node_settled", **fields, outcome=node.get("outcome"), seq=node.get("seq")
            )
        elif node["status"] == "scheduled":
            emit(con, run, "node_scheduled", **fields, handle=node.get("task_id"))
        else:
            emit(
                con,
                run,
                "node_status",
                **fields,
                **{"from": old.get("status"), "to": node["status"]},
            )
    if before["status"] != run["status"]:
        emit(con, run, "run_status", **{"from": before["status"], "to": run["status"]})
    for marker in run.get("notified", []):
        if marker not in before.get("notified", []):
            emit(con, run, "marker", marker=marker)
    if event and event.get("type") == "review":
        node = run["nodes"][event["node_id"]]
        emit(
            con,
            run,
            "review_decided",
            node_id=event["node_id"],
            decision=event["decision"],
            attempt_id=node.get("attempt_id"),
        )


def safe_emit(con, run, kind, **fields):
    if not run.get("observability", {}).get("trace_enabled"):
        return
    con.execute("SAVEPOINT optional_trace_event")
    try:
        emit(con, run, kind, **fields)
    except Exception:
        con.execute("ROLLBACK TO optional_trace_event")
    finally:
        con.execute("RELEASE optional_trace_event")


def read_trace(db, run_id):
    run = db.get_run(run_id)
    try:
        with db.connect() as con:
            lines = [
                row[0]
                for row in con.execute(
                    "SELECT event FROM py_traces WHERE run_id=? ORDER BY id", (run_id,)
                )
            ]
            # Preserve old exports without inventing historical observer events.
            if not lines and "observability" not in run and db.settings().get("trace_enabled"):
                lines = [
                    json.dumps(
                        redact(
                            {
                                "ts": row[0],
                                "run_id": run_id,
                                "kind": "legacy_event",
                                "event": json.loads(row[1]),
                            }
                        )
                    )
                    for row in con.execute(
                        "SELECT at,event FROM py_events WHERE run_id=? ORDER BY id", (run_id,)
                    )
                ]
    except sqlite3.Error:
        return None
    return "\n".join(lines) + "\n" if lines else None
