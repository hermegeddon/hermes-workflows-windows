"""SQLite persistence. Connections never cross threads; updates are transactional."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

ACTIVE = ("created", "running", "waiting")
TERMINAL = ("completed", "failed", "cancelled")
NODE_TERMINAL = ("completed", "failed", "skipped", "cancelled")


class ConflictError(ValueError):
    pass


def hermes_home() -> Path:
    # Hermes scopes profiles with a ContextVar; HERMES_HOME alone is insufficient
    # in a gateway serving several profiles in the same process.
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        pass
    else:
        return Path(get_hermes_home()).expanduser()
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else hermes_home() / "workflows" / "runs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS py_metadata (version INTEGER NOT NULL);
                INSERT INTO py_metadata SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM py_metadata);
                CREATE TABLE IF NOT EXISTS py_runs (
                    run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
                    status TEXT NOT NULL, state TEXT NOT NULL, spec TEXT NOT NULL,
                    started_at REAL NOT NULL, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS py_single_active_run ON py_runs(workflow_id)
                    WHERE status IN ('created','running','waiting');
                CREATE INDEX IF NOT EXISTS py_runs_recent ON py_runs(started_at DESC);
                CREATE TABLE IF NOT EXISTS py_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at REAL NOT NULL, event TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS py_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS py_schedules (
                    id TEXT PRIMARY KEY, workflow_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS py_exports (cache_key TEXT PRIMARY KEY, content TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
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

    def create_run(self, state: dict, spec: dict, max_active: int = 4) -> dict:
        with self.connect(write=True) as con:
            count = con.execute(
                "SELECT count(*) FROM py_runs WHERE status IN ('created','running','waiting')"
            ).fetchone()[0]
            if count >= max_active:
                raise ConflictError(f"maximum active runs ({max_active}) reached")
            try:
                con.execute(
                    "INSERT INTO py_runs(run_id,workflow_id,status,state,spec,started_at) VALUES(?,?,?,?,?,?)",
                    (
                        state["run_id"],
                        state["workflow_id"],
                        state["status"],
                        json.dumps(state),
                        json.dumps(spec),
                        state["started_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "workflow already has an active run, or run id already exists"
                ) from exc
            self._event(con, state["run_id"], {"type": "run_created", "status": state["status"]})
        return state

    @staticmethod
    def _event(con, run_id, event):
        con.execute(
            "INSERT INTO py_events(run_id,at,event) VALUES(?,?,?)",
            (run_id, time.time(), json.dumps(event)),
        )

    def get_run(self, run_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT state FROM py_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run not found")
        return json.loads(row[0])

    def get_spec(self, run_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT spec FROM py_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run not found")
        return json.loads(row[0])

    def mutate_run(
        self,
        run_id: str,
        change: Callable[[dict], None],
        *,
        event: dict | None = None,
        spec: dict | None = None,
        owner: str | None = None,
    ) -> dict:
        with self.connect(write=True) as con:
            row = con.execute(
                "SELECT state,lease_owner,lease_until FROM py_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("run not found")
            if owner and (row[1] != owner or row[2] < time.time()):
                raise ConflictError("run lease lost")
            state = json.loads(row[0])
            before = json.dumps(state)
            change(state)
            encoded = json.dumps(state)
            try:
                con.execute(
                    "UPDATE py_runs SET status=?,state=? WHERE run_id=?",
                    (state["status"], encoded, run_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("workflow already has another active run") from exc
            if spec is not None:
                con.execute("UPDATE py_runs SET spec=? WHERE run_id=?", (json.dumps(spec), run_id))
            if encoded != before:
                self._event(
                    con, run_id, event or {"type": "state_changed", "status": state["status"]}
                )
        return state

    def active_ids(self) -> list[str]:
        with self.connect() as con:
            return [
                r[0]
                for r in con.execute(
                    "SELECT run_id FROM py_runs WHERE status IN ('created','running','waiting')"
                )
            ]

    def claim(self, run_id: str, owner: str, seconds: float = 30) -> bool:
        now = time.time()
        with self.connect(write=True) as con:
            return (
                con.execute(
                    "UPDATE py_runs SET lease_owner=?,lease_until=? WHERE run_id=? "
                    "AND (lease_owner=? OR lease_until<?) AND status IN ('created','running','waiting')",
                    (owner, now + seconds, run_id, owner, now),
                ).rowcount
                == 1
            )

    def release(self, run_id: str, owner: str):
        with self.connect(write=True) as con:
            con.execute(
                "UPDATE py_runs SET lease_owner=NULL,lease_until=0 WHERE run_id=? AND lease_owner=?",
                (run_id, owner),
            )

    def list_runs(self, scope="active", workflow_id=None) -> list[dict]:
        clauses, args = [], []
        if scope != "all":
            clauses.append("status IN ('created','running','waiting')")
        if workflow_id is not None:
            clauses.append("workflow_id=?")
            args.append(workflow_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as con:
            states = [
                json.loads(r[0])
                for r in con.execute(
                    "SELECT state FROM py_runs" + where + " ORDER BY started_at DESC,run_id", args
                )
            ]
        result = []
        for state in states:
            start, end = state.get("started_at"), state.get("finished_at")
            telemetry = [n["telemetry"] for n in state["nodes"].values() if n.get("telemetry")]
            result.append(
                {
                    "run_id": state["run_id"],
                    "workflow_id": state["workflow_id"],
                    "project_id": state.get("project_id"),
                    "status": state["status"],
                    "current_node": next(
                        (
                            n["node_id"]
                            for n in state["nodes"].values()
                            if n["status"] in ("running", "scheduled", "waiting_for_review")
                        ),
                        None,
                    ),
                    "started_at": start,
                    "finished_at": end,
                    "duration": end - start if start is not None and end is not None else None,
                    "total_tokens": sum(n.get("total_tokens", 0) for n in telemetry)
                    if telemetry
                    else None,
                }
            )
        return result

    def trace(self, run_id: str) -> str:
        self.get_run(run_id)
        with self.connect() as con:
            rows = con.execute(
                "SELECT at,event FROM py_events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return "".join(json.dumps({"at": r[0], **json.loads(r[1])}) + "\n" for r in rows)

    def settings(self) -> dict:
        with self.connect() as con:
            return {
                r[0]: json.loads(r[1]) for r in con.execute("SELECT key,value FROM py_settings")
            }

    def export_cache(self, cache_key: str, value: dict | None = None) -> dict | None:
        with self.connect(write=value is not None) as con:
            if value is not None:
                con.execute(
                    "INSERT OR REPLACE INTO py_exports VALUES(?,?)", (cache_key, json.dumps(value))
                )
            row = con.execute(
                "SELECT content FROM py_exports WHERE cache_key=?", (cache_key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_settings(self, values: dict):
        with self.connect(write=True) as con:
            con.executemany(
                "INSERT INTO py_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v)) for k, v in values.items()],
            )

    def schedules(self) -> list[dict]:
        with self.connect() as con:
            return [
                json.loads(r[0])
                for r in con.execute("SELECT data FROM py_schedules ORDER BY workflow_id")
            ]

    def schedule(self, job_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT data FROM py_schedules WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("schedule not found")
        return json.loads(row[0])

    def put_schedule(self, data: dict):
        with self.connect(write=True) as con:
            con.execute(
                "INSERT INTO py_schedules VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (data["hermes_cron_id"], data["workflow_id"], json.dumps(data)),
            )

    def delete_schedule(self, job_id: str):
        with self.connect(write=True) as con:
            if not con.execute("DELETE FROM py_schedules WHERE id=?", (job_id,)).rowcount:
                raise KeyError("schedule not found")
