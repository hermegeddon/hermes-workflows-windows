"""Transactional SQLite migrations. Never rewrite historical run/spec JSON."""

from __future__ import annotations

import json
import time
import uuid

SCHEMA_VERSION = 5

V1 = """
CREATE TABLE py_runs (
    run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
    status TEXT NOT NULL, state TEXT NOT NULL, spec TEXT NOT NULL,
    started_at REAL NOT NULL, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX py_single_active_run ON py_runs(workflow_id)
    WHERE status IN ('created','running','waiting');
CREATE INDEX py_runs_recent ON py_runs(started_at DESC);
CREATE TABLE py_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    at REAL NOT NULL, event TEXT NOT NULL
);
CREATE TABLE py_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE py_schedules (
    id TEXT PRIMARY KEY, workflow_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL
);
CREATE TABLE py_exports (cache_key TEXT PRIMARY KEY, content TEXT NOT NULL);
"""

V2 = """
ALTER TABLE py_runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE py_attempts (
    attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES py_runs(run_id),
    node_id TEXT NOT NULL, revision INTEGER NOT NULL, iteration INTEGER NOT NULL,
    attempt INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
    handle TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(run_id,node_id,iteration,attempt)
);
CREATE INDEX py_attempts_run ON py_attempts(run_id,revision);
CREATE TABLE py_effects (
    effect_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES py_runs(run_id),
    effect_key TEXT NOT NULL, revision INTEGER NOT NULL, node_id TEXT,
    attempt_id TEXT REFERENCES py_attempts(attempt_id), kind TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL, driver_owner TEXT,
    handle TEXT, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(run_id,effect_key)
);
"""

V3 = """
CREATE TABLE py_notifications (
    notice_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES py_runs(run_id),
    revision INTEGER NOT NULL, notice_key TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
    error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(run_id,notice_key)
);
CREATE INDEX py_notifications_pending ON py_notifications(status,created_at);
"""

V4 = """
CREATE TABLE py_workers (
    worker_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES py_runs(run_id),
    node_id TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES py_attempts(attempt_id),
    revision INTEGER NOT NULL, stream_key TEXT NOT NULL, active INTEGER NOT NULL,
    context TEXT NOT NULL, data TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX py_workers_active ON py_workers(attempt_id,stream_key) WHERE active=1;
CREATE TABLE py_worker_approvals (
    request_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES py_workers(worker_id),
    token TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, choice TEXT,
    expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX py_worker_approvals_pending ON py_worker_approvals(worker_id,status);
CREATE TABLE py_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES py_runs(run_id),
    event TEXT NOT NULL
);
CREATE INDEX py_traces_run ON py_traces(run_id,id);
"""


def statements(con, sql):
    # executescript() implicitly commits, which would break migration rollback.
    for statement in sql.split(";"):
        if statement.strip():
            con.execute(statement)


def migrate(con):
    """Called under BEGIN IMMEDIATE, including on simultaneous first startup."""
    con.execute("CREATE TABLE IF NOT EXISTS py_metadata (version INTEGER NOT NULL)")
    versions = [row[0] for row in con.execute("SELECT version FROM py_metadata")]
    if not versions:
        statements(con, V1)
        con.execute("INSERT INTO py_metadata VALUES(1)")
        version = 1
    elif len(versions) == 1 and type(versions[0]) is int and 1 <= versions[0] <= SCHEMA_VERSION:
        version = versions[0]
    else:
        raise ValueError(f"unsupported workflow database version: {versions}")
    if version == 1:
        statements(con, V2)
        now = time.time()
        for row in con.execute("SELECT run_id,state FROM py_runs").fetchall():
            run = json.loads(row[1])
            for node_id, state in run["nodes"].items():
                if state["status"] in ("pending", "skipped"):
                    continue
                if state["status"] == "cancelled" and not state.get("started_at"):
                    continue
                con.execute(
                    "INSERT INTO py_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        row[0],
                        node_id,
                        0,
                        0,
                        1,
                        state["status"],
                        json.dumps(state),
                        None,
                        state.get("started_at") or now,
                        now,
                    ),
                )
        con.execute("UPDATE py_metadata SET version=2")
        version = 2
    if version == 2:
        statements(con, V3)
        con.execute("UPDATE py_metadata SET version=3")
        version = 3
    if version == 3:
        statements(con, V4)
        con.execute("UPDATE py_metadata SET version=4")
        version = 4
    if version == 4:
        # Keep a still-running older supervisor from reintroducing a local due
        # time after native handoff, including a legacy spec-trigger rewrite.
        con.execute("""
            CREATE TRIGGER py_schedule_authority_guard BEFORE UPDATE OF data ON py_schedules
            WHEN (json_extract(OLD.data,'$.authority')='native' AND
                (coalesce(json_extract(NEW.data,'$.authority'),'') NOT IN ('native','standalone')
                 OR json_extract(NEW.data,'$.generation') IS NULL))
              OR (json_extract(NEW.data,'$.authority')='native'
                  AND json_extract(NEW.data,'$.next_run') IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'native schedule authority cannot be overwritten by a legacy writer'); END
        """)
        con.execute("UPDATE py_metadata SET version=5")
