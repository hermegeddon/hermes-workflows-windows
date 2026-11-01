"""Durable native cards; Hermes' existing gateway dispatcher owns workers.

Card creation is reconciled by a stable attempt key inside a native board write
transaction. This is stronger than the host's stand-alone idempotency lookup,
which excludes archived tasks and does not serialize its initial lookup.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .db import TERMINAL, ConflictError
from .effects import EffectStore
from .executor import Result
from .native import NativeUnavailable

log = logging.getLogger(__name__)
CREATOR = "hermes-workflows-py"
KIND = "kanban_card"


def board_for(workflow, project_id=None):
    scope = workflow["scope"]
    if scope["type"] not in ("project", "projects"):
        return None
    projects = scope.get("projects") or []
    slug = project_id or (projects[0] if projects else None)
    if slug is None:
        return None
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", slug.lower()):
        raise ValueError(
            "project board must be a native board slug (1-64 letters, digits, hyphens or underscores)"
        )
    return slug.lower()


def on_board(node, board):
    return (
        node["type"] == "agent_task"
        and (bool(board) or node.get("adopt") is True)
        and node.get("board") is not False
    )


def completion(task, latest):
    """Read task_runs, not session IDs: native compression can rotate sessions."""
    if task["status"] not in ("done", "archived"):
        return None
    output = (latest["summary"] if latest else None) or task["result"] or ""
    override = None
    if latest:
        try:
            metadata = json.loads(latest["metadata"] or "{}")
            override = metadata.get("node_outcome") if isinstance(metadata, dict) else None
        except (ValueError, TypeError):
            pass
    # A summary self-report only classifies a completed native worker.
    # It cannot turn a crashed worker into success. Native metadata has
    # separate, explicit precedence in the upstream completion contract.
    if override not in ("success", "failure") and latest and latest["outcome"] == "completed":
        tokens = re.findall(r'\{\s*"node_outcome"\s*:\s*"(success|failure)"\s*\}', output)
        override = tokens[-1] if tokens else None
    native_outcome = latest["outcome"] if latest else None
    success = native_outcome in (None, "completed") and task["status"] == "done"
    sentinel = re.search(
        r"API call failed after \d+ retr(?:y|ies)|HTTP\s*(?:429|50[234])|"
        r"temporarily overloaded|overloaded_error|usage limit|connection reset",
        output,
        re.I,
    )
    if override in ("success", "failure"):
        success = override == "success"
    elif sentinel:
        success = False
    return Result(
        "success" if success else "failure",
        output,
        None if success else f"native Kanban worker failed ({native_outcome or task['status']})",
    )


class KanbanExecutor:
    def __init__(self, db, host, *, poll_seconds=1):
        self.db, self.host = db, host
        self.effects = EffectStore(db)
        self.poll_seconds = poll_seconds
        self.adoption = None

    @contextmanager
    def board(self, slug, *, expected=None, ensure=False):
        with self.host.scope():
            kb = self.host.module("kanban")
            path = Path(kb.kanban_db_path(board=slug)).resolve()
            canonical = (
                Path(kb.kanban_home()) / "kanban.db"
                if slug == "default"
                else Path(kb.board_dir(slug)) / "kanban.db"
            ).resolve()
            if path != canonical or expected is not None and str(path) != expected:
                raise NativeUnavailable("native board path changed or is pinned to another board")
            if ensure:
                matches = [b for b in kb.list_boards(include_archived=True) if b["slug"] == slug]
                if matches and matches[0].get("archived"):
                    raise NativeUnavailable("the workflow board is archived; restore it explicitly")
                if not matches:
                    kb.create_board(slug)
            conn = self.host.module("kanban_store").connect(board=slug)
            try:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
                if not {"idempotency_key", "workflow_template_id", "current_step_key"} <= columns:
                    raise NativeUnavailable("native Kanban lacks durable workflow card columns")
                yield kb, conn, str(path)
            finally:
                conn.close()

    def _current(self, run_id, owner, revision, node_id, attempt_id):
        state, _, _ = self.db.execution(run_id, owner=owner, revision=revision)
        if Path(state["nodes"][node_id].get("executor_home", self.host.home)) != self.host.home:
            raise ConflictError("native card belongs to a different profile home")
        if state["status"] in TERMINAL or state["nodes"][node_id].get("attempt_id") != attempt_id:
            raise ConflictError("Kanban attempt was superseded")
        return state

    @staticmethod
    def _check(task, key, workflow_id, node_id):
        if task is None or (
            task["created_by"],
            task["idempotency_key"],
            task["workflow_template_id"],
            task["current_step_key"],
        ) != (CREATOR, key, workflow_id, node_id):
            raise ConflictError("native card ownership does not match the workflow attempt")

    def schedule(self, node, run, board, *, owner, revision, attempt_id, timeout):
        unsupported = {
            "enabled_toolsets",
            "disabled_toolsets",
            "max_iterations",
            "max_tokens",
            "load_external_secrets",
        } & node.keys()
        if unsupported:
            raise NativeUnavailable(
                "native cards cannot represent these node overrides; use board:false: "
                + ", ".join(sorted(unsupported))
            )
        args = (run["run_id"], owner, revision, node["id"], attempt_id)
        self._current(*args)
        home_key = hashlib.sha256(str(self.host.home).encode()).hexdigest()[:16]
        key = f"{CREATOR}:{home_key}:{run['run_id']}:{attempt_id}"
        with self.db.connect() as conn:
            previous = conn.execute(
                "SELECT payload FROM py_effects WHERE run_id=? AND effect_key=?",
                (run["run_id"], key),
            ).fetchone()
        expected = json.loads(previous[0])["path"] if previous else None
        # Exact board path is part of the durable intent; never reconnect a
        # recovered attempt to a similarly named board on a different host.
        with self.board(board, expected=expected, ensure=True) as (_, _, path):
            pass
        payload = {
            "home": str(self.host.home),
            "board": board,
            "path": path,
            "key": key,
            "workflow_id": run["workflow_id"],
            "node_id": node["id"],
            "node": node,
            "timeout": timeout,
        }
        effect = self.effects.prepare(
            run["run_id"],
            key,
            KIND,
            payload,
            owner=owner,
            revision=revision,
            node_id=node["id"],
            attempt_id=attempt_id,
        )
        if effect["status"] == "prepared":
            effect = self.effects.begin(effect["effect_id"], owner=owner, revision=revision)
        with self.board(board, expected=path) as (kb, conn, _):
            with kb.write_txn(conn):
                self._current(*args)
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
                ).fetchall()
                if len(rows) > 1:
                    raise ConflictError("multiple native cards share this attempt identity")
                if rows:
                    task_id = rows[0]["id"]
                    self._check(rows[0], key, run["workflow_id"], node["id"])
                elif effect["status"] in ("completed", "settled", "cancelled"):
                    raise ConflictError("persisted native card is missing; refusing to recreate it")
                else:
                    task_id = kb.create_task(
                        conn,
                        title=node.get("title") or node["id"],
                        body=node["prompt"],
                        assignee=node["profile"],
                        created_by=CREATOR,
                        idempotency_key=key,
                        workspace_kind="dir" if node.get("workdir") else "scratch",
                        workspace_path=node.get("workdir"),
                        board=board,
                        project_id="",
                        model_override=node.get("model"),
                        skills=node.get("skills"),
                        max_retries=node.get("max_retries"),
                        max_runtime_seconds=timeout,
                    )
                    conn.execute(
                        "UPDATE tasks SET workflow_template_id=?,current_step_key=? WHERE id=?",
                        (run["workflow_id"], node["id"], task_id),
                    )
                # Native create_task uses a nested savepoint. Only this outer
                # commit publishes the card AND workflow metadata to dispatch.
        handle = {"kind": KIND, "board": board, "path": path, "task_id": task_id, "key": key}
        self._current(*args)
        if effect["status"] == "unknown":
            self.effects.resolve(
                effect["effect_id"], handle, completed=True, owner=owner, revision=revision
            )
        elif effect["status"] == "in_flight":
            self.effects.attach_handle(effect["effect_id"], handle, owner=owner, revision=revision)
            self.effects.complete(effect["effect_id"], handle, owner=owner, revision=revision)
        self.db.attach_attempt_handle(
            run["run_id"], node["id"], attempt_id, handle, owner=owner, revision=revision
        )
        self.db.mutate_run(
            run["run_id"],
            lambda r: r["nodes"][node["id"]].update(task_id=task_id, board=board),
            owner=owner,
            revision=revision,
            attempt=(node["id"], attempt_id),
        )
        return handle

    async def execute(self, node, run, board, *, owner, revision, attempt_id, timeout):
        if node.get("adopt"):
            from .adoption import AdoptionExecutor

            if self.adoption is None:
                self.adoption = AdoptionExecutor(self)
            return await self.adoption.execute(
                node,
                run,
                board,
                owner=owner,
                revision=revision,
                attempt_id=attempt_id,
                timeout=timeout,
            )
        try:
            handle = self.schedule(
                node,
                run,
                board,
                owner=owner,
                revision=revision,
                attempt_id=attempt_id,
                timeout=timeout,
            )
            started = run["nodes"][node["id"]]["started_at"]
            while True:
                self._current(run["run_id"], owner, revision, node["id"], attempt_id)
                with self.board(board, expected=handle["path"]) as (_, conn, _):
                    task = conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (handle["task_id"],)
                    ).fetchone()
                    self._check(task, handle["key"], run["workflow_id"], node["id"])
                    latest = conn.execute(
                        "SELECT outcome,summary,metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (handle["task_id"],),
                    ).fetchone()
                    result = completion(task, latest)
                if result is not None:
                    return result
                if time.time() - started >= timeout:
                    return Result(
                        "failure", error="native Kanban node timed out while queued or running"
                    )
                await asyncio.sleep(self.poll_seconds)
        finally:
            # Runtime shutdown/lease loss leaves active cards for the next
            # owner. Explicit cancellation/retry/finish gets durable cleanup.
            self.cleanup(run_id=run["run_id"])

    def cleanup(self, *, run_id=None):
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT e.*,r.state as run_state,r.revision as current_revision FROM py_effects e JOIN py_runs r ON r.run_id=e.run_id "
                "WHERE e.kind=? AND e.status NOT IN ('settled','cancelled') "
                + ("AND e.run_id=? " if run_id else "")
                + "ORDER BY e.created_at",
                (KIND, run_id) if run_id else (KIND,),
            ).fetchall()
        for effect in rows:
            payload = json.loads(effect["payload"])
            if payload.get("home") != str(self.host.home):
                log.error(
                    "native card cleanup refused a different profile home for %s",
                    effect["effect_id"],
                )
                continue
            state = json.loads(effect["run_state"])
            node = state["nodes"][effect["node_id"]]
            if (
                state["status"] not in TERMINAL
                and effect["current_revision"] == effect["revision"]
                and node.get("attempt_id") == effect["attempt_id"]
                and node["status"] not in ("completed", "failed", "cancelled", "skipped")
            ):
                continue
            try:
                with self.board(payload["board"], expected=payload["path"]) as (kb, conn, _):
                    # Serialize the lookup with any old creator's commit. A
                    # cancellation must not mark an unacknowledged card absent
                    # while that card is still in an open board transaction.
                    with kb.write_txn(conn):
                        tasks = conn.execute(
                            "SELECT * FROM tasks WHERE idempotency_key=?", (payload["key"],)
                        ).fetchall()
                    for task in tasks:
                        self._check(
                            task, payload["key"], payload["workflow_id"], payload["node_id"]
                        )
                        if task["status"] not in ("done", "archived"):
                            kb.archive_task(conn, task["id"])
                with self.db.connect(write=True) as conn:
                    conn.execute(
                        "UPDATE py_effects SET status='settled',updated_at=? WHERE effect_id=?",
                        (time.time(), effect["effect_id"]),
                    )
            except Exception:
                log.exception("owned native card cleanup failed for effect %s", effect["effect_id"])
