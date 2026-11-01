"""SQLite persistence. Connections never cross threads; updates are transactional."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .migrations import migrate

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


def _enable_wal(con, *, timeout=15):
    # Switching journal mode can return BUSY immediately despite busy_timeout
    # when several hosts initialize/migrate the same DELETE-mode database.
    # Retry that operation explicitly, with a bounded wall-clock budget.
    original_timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
    deadline = time.monotonic() + timeout
    try:
        con.execute("PRAGMA busy_timeout=0")
        while True:
            try:
                con.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                remaining = deadline - time.monotonic()
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))
    finally:
        con.execute(f"PRAGMA busy_timeout={int(original_timeout)}")


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else hermes_home() / "workflows" / "runs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            _enable_wal(con)
        with self.connect(write=True) as con:
            migrate(con)

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
        state = {**state, "execution_revision": 0}
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

    @staticmethod
    def _owned_row(con, run_id, owner=None, revision=None, attempt=None):
        row = con.execute("SELECT * FROM py_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run not found")
        if owner is not None and (row["lease_owner"] != owner or row["lease_until"] < time.time()):
            raise ConflictError("run lease lost")
        if revision is not None and row["revision"] != revision:
            raise ConflictError("run execution was superseded")
        if attempt is not None:
            state = json.loads(row["state"])["nodes"].get(attempt[0], {})
            if state.get("attempt_id") != attempt[1]:
                raise ConflictError("node attempt was superseded")
        return row

    def execution(self, run_id, *, owner=None, revision=None):
        """Read the state, immutable spec and revision from one SQLite snapshot."""
        with self.connect() as con:
            row = self._owned_row(con, run_id, owner, revision)
            return json.loads(row["state"]), json.loads(row["spec"]), row["revision"]

    def mutate_run(
        self,
        run_id: str,
        change: Callable[[dict], None],
        *,
        event: dict | None = None,
        spec: dict | None = None,
        owner: str | None = None,
        revision: int | None = None,
        attempt: tuple[str, str] | None = None,
        invalidate: bool = False,
        restart_attempts: dict[str, dict] | None = None,
    ) -> dict:
        with self.connect(write=True) as con:
            row = self._owned_row(con, run_id, owner, revision, attempt)
            state = json.loads(row["state"])
            before = json.dumps(state)
            change(state)
            changed = json.dumps(state) != before or spec is not None
            revoked = invalidate and changed
            next_revision = row["revision"] + int(revoked)
            if revoked:
                state["execution_revision"] = next_revision
            self._sync_attempts(
                con, json.loads(before), state, next_revision, restart_attempts or {}
            )
            encoded = json.dumps(state)
            try:
                con.execute(
                    "UPDATE py_runs SET status=?,state=?,revision=?,"
                    "lease_owner=CASE WHEN ? THEN NULL ELSE lease_owner END,"
                    "lease_until=CASE WHEN ? THEN 0 ELSE lease_until END WHERE run_id=?",
                    (state["status"], encoded, next_revision, revoked, revoked, run_id),
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

    def _sync_attempts(self, con, before, run, revision, restarts):
        now = time.time()
        for node_id, state in run["nodes"].items():
            old = before["nodes"].get(node_id, {})
            if old == state and node_id not in restarts:
                continue
            if (
                state["status"] == "cancelled"
                and old.get("status") == "pending"
                and not old.get("attempt_id")
            ):
                # Cancelling an unstarted branch is not an execution attempt.
                continue
            if node_id in restarts and old.get("attempt_id"):
                archived = {**old, **restarts[node_id], "status": "completed", "finished_at": now}
                con.execute(
                    "UPDATE py_attempts SET status='completed',data=?,updated_at=? WHERE attempt_id=?",
                    (json.dumps(archived), now, old["attempt_id"]),
                )
                for key in ("attempt_id", "outcome", "output", "error", "telemetry", "finished_at"):
                    state.pop(key, None)
            if state["status"] in ("pending", "skipped"):
                if old.get("attempt_id") and old["status"] not in NODE_TERMINAL:
                    archived = {**old, "status": "interrupted", "finished_at": now}
                    con.execute(
                        "UPDATE py_attempts SET status='interrupted',data=?,updated_at=? WHERE attempt_id=?",
                        (json.dumps(archived), now, old["attempt_id"]),
                    )
                continue
            iteration = state.get("iteration", 0)
            identity = state.get("attempt_id")
            # A legacy active node has a backfilled history row but no added
            # fields in its original JSON. Adopt that row only when it changes.
            if (
                not identity
                and old.get("status") not in (None, "pending", "skipped")
                and node_id not in restarts
            ):
                legacy = con.execute(
                    "SELECT attempt_id,attempt FROM py_attempts WHERE run_id=? AND node_id=? "
                    "AND iteration=? ORDER BY attempt DESC LIMIT 1",
                    (run["run_id"], node_id, iteration),
                ).fetchone()
                if legacy:
                    identity, state["attempt"] = legacy
            if not identity or old.get("status") == "pending":
                identity = uuid.uuid4().hex
                number = con.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM py_attempts WHERE run_id=? AND node_id=? AND iteration=?",
                    (run["run_id"], node_id, iteration),
                ).fetchone()[0]
                state["attempt"] = number
                con.execute(
                    "INSERT INTO py_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        run["run_id"],
                        node_id,
                        revision,
                        iteration,
                        number,
                        state["status"],
                        "{}",
                        None,
                        now,
                        now,
                    ),
                )
            state.update(attempt_id=identity, iteration=iteration)
            con.execute(
                "UPDATE py_attempts SET status=?,data=?,updated_at=? WHERE attempt_id=? AND run_id=? AND node_id=?",
                (state["status"], json.dumps(state), now, identity, run["run_id"], node_id),
            )

    def attempts(self, run_id: str) -> list[dict]:
        self.get_run(run_id)
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM py_attempts WHERE run_id=? ORDER BY node_id,iteration,attempt",
                (run_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "data": json.loads(row["data"]),
                "handle": json.loads(row["handle"]) if row["handle"] else None,
            }
            for row in rows
        ]

    def attach_attempt_handle(self, run_id, node_id, attempt_id, handle, *, owner, revision):
        with self.connect(write=True) as con:
            self._owned_row(con, run_id, owner, revision, (node_id, attempt_id))
            row = con.execute(
                "SELECT handle FROM py_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            encoded = json.dumps(handle, sort_keys=True)
            if row is None:
                raise KeyError("attempt not found")
            if row[0] is not None and row[0] != encoded:
                raise ConflictError("attempt already has a different external handle")
            con.execute(
                "UPDATE py_attempts SET handle=?,updated_at=? WHERE attempt_id=?",
                (encoded, time.time(), attempt_id),
            )

    def active_ids(self) -> list[str]:
        with self.connect() as con:
            return [
                r[0]
                for r in con.execute(
                    "SELECT run_id FROM py_runs WHERE status IN ('created','running','waiting')"
                )
            ]

    def claim(self, run_id: str, owner: str, seconds: float = 30, *, revision=None) -> bool:
        now = time.time()
        with self.connect(write=True) as con:
            return (
                con.execute(
                    "UPDATE py_runs SET lease_owner=?,lease_until=? WHERE run_id=? "
                    "AND (lease_owner=? OR lease_until<?) AND status IN ('created','running','waiting') "
                    "AND (? IS NULL OR revision=?)",
                    (owner, now + seconds, run_id, owner, now, revision, revision),
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
