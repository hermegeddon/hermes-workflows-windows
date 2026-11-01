"""Asynchronous graph execution with persistent review gates and restart recovery."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from functools import partial
from graphlib import TopologicalSorter

from . import settings as settings_module
from .adoption import AdoptionError, extract_task_ids
from .db import ACTIVE, NODE_TERMINAL, TERMINAL, ConflictError, Database, hermes_home
from .effects import EffectStore
from .executor import Executor, Result
from .graph import analyse, ancestors
from .kanban import KanbanExecutor, board_for, on_board
from .native import NativeHost
from .specs import REVIEWS, dependencies, fill_params, require_valid, resolve


def _initial_node(node: dict) -> dict:
    return {"node_id": node["id"], "node_type": node["type"], "status": "pending"}


def selected_edges(workflow: dict, run: dict, source: str) -> list[dict]:
    state = run["nodes"][source]
    if state["status"] == "skipped" or (
        state["status"] != "completed" and "review_decision" not in state
    ):
        return []
    edges = [e for e in workflow["edges"] if e["from"] == source]
    plain = [e for e in edges if not e.get("condition") and not e.get("fallback")]
    conditioned = [e for e in edges if e.get("condition")]

    def matches(edge):
        condition = edge["condition"]
        if condition["type"] == "review_status":
            return run["nodes"][source].get("review_decision") == condition["equals"]
        return run["nodes"][condition["node"]].get("outcome") == condition["equals"]

    if not conditioned:
        return plain
    passed = [e for e in conditioned if matches(e)]
    return (passed if passed else [e for e in edges if e.get("fallback")]) + plain


class Engine:
    def __init__(self, db: Database, executor: Executor | None = None):
        self.db = db
        self.executor = executor or Executor()
        self.kanban = None

    def board_executor(self):
        if self.kanban is None:
            self.kanban = KanbanExecutor(self.db, NativeHost(self.executor.home or hermes_home()))
        return self.kanban

    def reconcile_external(self):
        # No host imports when there are no native card intents.
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM py_effects WHERE kind='kanban_card' AND status!='settled' LIMIT 1"
            ).fetchone()
        if exists:
            self.board_executor().cleanup()

    def start(
        self,
        workflow: dict,
        *,
        workflow_path: str | None = None,
        project_id: str | None = None,
        input: str | None = None,
        params: dict | None = None,
        run_id: str | None = None,
        origin: str | None = None,
        origin_metadata: dict | None = None,
    ) -> dict:
        require_valid(workflow)
        if not workflow.get("enabled", True):
            raise ConflictError("workflow is disabled")
        config = settings_module.values(self.db)
        if any(n["type"] == "script" for n in workflow["nodes"]) and not config["scripts_enabled"]:
            raise ConflictError("script execution is disabled; enable scripts_enabled in Settings")
        if input is not None and not isinstance(input, str):
            raise ValueError("input must be a string")
        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("project_id must be a string")
        if origin is not None and (not isinstance(origin, str) or not origin.strip()):
            raise ValueError("origin must be a non-empty string")
        board = board_for(workflow, project_id)
        state = {
            "run_id": run_id or f"{workflow['id']}-{uuid.uuid4().hex[:12]}",
            "workflow_id": workflow["id"],
            "workflow_version": workflow["version"],
            "status": "created",
            "started_at": time.time(),
            "finished_at": None,
            "nodes": {n["id"]: _initial_node(n) for n in workflow["nodes"]},
            "params": fill_params(workflow, params if params is not None else {}),
            "observability": {"trace_enabled": config["trace_enabled"]},
            "notification_context": {
                "home": str(self.executor.home or self.db.path.parent.parent),
                "default": config["default_deliver"]
                or os.environ.get("HERMES_WORKFLOWS_DELIVER")
                or None,
                "routing": copy.deepcopy(origin_metadata or {}),
            },
        }
        if origin:
            state["origin"] = origin
        if workflow_path:
            state["workflow_path"] = workflow_path
        if project_id or board:
            # Keep the implicit project binding visible in run lists too.
            state["project_id"] = project_id or workflow["scope"]["projects"][0]
        if board:
            state["board"] = board
        if input and input.strip():
            state["input"] = input.strip()
        return self.db.create_run(state, workflow, config["max_parallel_runs"])

    def cancel(self, run_id: str) -> dict:
        def change(run):
            if run["status"] in TERMINAL:
                return
            run.update(status="cancelled", finished_at=time.time())
            for state in run["nodes"].values():
                if state["status"] not in NODE_TERMINAL:
                    state.update(status="cancelled", finished_at=time.time())

        state = self.db.mutate_run(run_id, change, event={"type": "run_cancelled"}, invalidate=True)
        self.reconcile_external()
        return state

    def review(
        self,
        run_id: str,
        node_id: str,
        decision: str,
        note: str | None = None,
        *,
        attempt_id: str | None = None,
        _chat_reply: dict | None = None,
    ) -> dict:
        if decision not in REVIEWS:
            raise ValueError("decision must be approved, rejected or needs_changes")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string")
        if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id):
            raise ValueError("attempt_id must be a non-empty string")
        _, workflow, revision = self.db.execution(run_id)
        node = next((n for n in workflow["nodes"] if n["id"] == node_id), None)
        if not node or node["type"] != "human_review":
            raise ValueError("node is not a human review")
        if decision not in node.get("options", REVIEWS):
            raise ValueError("decision is not allowed by this review gate")

        def change(run):
            state = run["nodes"][node_id]
            if run["status"] not in ACTIVE or state["status"] != "waiting_for_review":
                raise ConflictError("review gate is not waiting for a decision")
            state.update(
                status="completed",
                outcome="success",
                review_decision=decision,
                seq=self._next_seq(run),
                finished_at=time.time(),
            )
            if note is not None:
                state["review_note"] = note
            run["status"] = "running"

        event = {"type": "review", "node_id": node_id, "decision": decision}
        guard = None
        if _chat_reply is not None:
            from .gate_reply import guard_reply

            if attempt_id is None:
                raise ValueError("chat reviews require an attempt identity")
            event["chat_reply_id"] = _chat_reply["identity"]
            guard = partial(guard_reply, node_id=node_id, reply=_chat_reply)
        state = self.db.mutate_run(
            run_id,
            change,
            event=event,
            revision=revision,
            attempt=(node_id, attempt_id) if attempt_id is not None else None,
            guard=guard,
        )
        return {
            "run_id": run_id,
            "node_id": node_id,
            "decision": decision,
            "status": state["status"],
        }

    def retry(self, run_id: str, workflow: dict, node_id: str | None = None) -> dict:
        require_valid(workflow)
        old = self.db.get_spec(run_id)

        def shape(wf):
            return (
                {n["id"]: n["type"] for n in wf["nodes"]},
                {(e["from"], e["to"]) for e in wf["edges"]},
            )

        if shape(old) != shape(workflow):
            raise ConflictError("workflow graph changed; start a new run")
        if not workflow.get("enabled", True):
            raise ConflictError("workflow is disabled")
        if (
            any(n["type"] == "script" for n in workflow["nodes"])
            and not settings_module.values(self.db)["scripts_enabled"]
        ):
            raise ConflictError("script execution is disabled")
        targets = {n["id"] for n in workflow["nodes"]}
        if node_id is not None:
            if not isinstance(node_id, str) or node_id not in targets:
                raise ValueError("node_id must name an existing node")
            targets = {node_id}
            while True:
                expanded = targets | {e["to"] for e in workflow["edges"] if e["from"] in targets}
                if expanded == targets:
                    break
                targets = expanded

        def change(run):
            if run["status"] not in ("failed", "cancelled", "waiting"):
                raise ConflictError("only failed, cancelled or waiting runs can be retried")
            if node_id is not None and run["nodes"][node_id]["status"] == "skipped":
                raise ConflictError("cannot retry an unselected branch; restart the whole run")
            for node in workflow["nodes"]:
                if node["id"] in targets:
                    iteration = run["nodes"][node["id"]].get("iteration", 0)
                    run["nodes"][node["id"]] = {**_initial_node(node), "iteration": iteration}
            run.update(status="created", finished_at=None, workflow_version=workflow["version"])
            run.pop("error", None)
            run.pop("finish_outcome", None)
            run["loop_reentries"] = 0

        state = self.db.mutate_run(
            run_id,
            change,
            event={"type": "run_retried", "node_id": node_id},
            spec=workflow,
            invalidate=True,
        )
        self.reconcile_external()
        return state

    @staticmethod
    def _next_seq(run):
        value = (
            max(
                run.get("completion_seq", 0),
                max((s.get("seq", 0) for s in run["nodes"].values()), default=0),
            )
            + 1
        )
        run["completion_seq"] = value
        return value

    @staticmethod
    def _reentries(run, workflow):
        return {
            edge["to"]
            for node in workflow["nodes"]
            for edge in selected_edges(workflow, run, node["id"])
            if run["nodes"][edge["to"]]["status"] in NODE_TERMINAL
            and (
                run["nodes"][node["id"]].get("seq", 0) > run["nodes"][edge["to"]].get("seq", 0)
                or node["id"] == edge["to"]
            )
        }

    @staticmethod
    def _finish(run, outcome, *, error=None):
        now = time.time()
        run.update(status="completed" if outcome == "success" else "failed", finished_at=now)
        if error:
            run["error"] = error
        for state in run["nodes"].values():
            if state["status"] not in NODE_TERMINAL:
                state.update(
                    status="skipped" if state["status"] == "pending" else "cancelled",
                    finished_at=now,
                )

    def _plan(self, run: dict, workflow: dict) -> list[str]:
        if run["status"] in TERMINAL:
            return []
        now = time.time()
        raw_graph, graph, _, _, _ = analyse(workflow)
        nodes = {n["id"]: n for n in workflow["nodes"]}
        for name, node in nodes.items():
            state = run["nodes"][name]
            if (
                node["type"] == "human_review"
                and state["status"] == "waiting_for_review"
                and "review_decision" in state
            ):
                state.update(status="completed", outcome="success", finished_at=now)
                if "seq" not in state:
                    state["seq"] = self._next_seq(run)
        if any(state.get("abort_run") for state in run["nodes"].values()):
            self._finish(run, "failure", error="node requested a hard workflow stop")
            return []
        resets = self._reentries(run, workflow)
        # An already-routed finish wins over a simultaneously selected back
        # edge. Otherwise resetting its parent could hide the finish token.
        finishing = []
        for name, node in nodes.items():
            if node["type"] != "finish" or (
                run["nodes"][name]["status"] != "pending" and name not in resets
            ):
                continue
            if all(
                run["nodes"][parent]["status"] in NODE_TERMINAL for parent in graph[name]
            ) and any(
                edge["to"] == name
                for parent in raw_graph[name]
                for edge in selected_edges(workflow, run, parent)
            ):
                finishing.append(name)
        if finishing:
            for name in finishing:
                run["nodes"][name].update(
                    status="completed",
                    outcome=nodes[name].get("outcome", "success"),
                    finished_at=now,
                    seq=self._next_seq(run),
                )
            outcome = (
                "failure"
                if any(nodes[name].get("outcome") == "failure" for name in finishing)
                else "success"
            )
            run["finish_outcome"] = outcome
            self._finish(run, outcome)
            return []
        if resets:
            limit = settings_module.values(self.db)["max_loop_reentries"]
            for name in nodes:
                if name not in resets:
                    continue
                state = run["nodes"][name]
                executed = state["status"] != "skipped"
                if executed:
                    if run.get("loop_reentries", 0) >= limit:
                        self._finish(run, "failure", error=f"loop re-entry limit ({limit}) reached")
                        return []
                    run["loop_reentries"] = run.get("loop_reentries", 0) + 1
                run["nodes"][name] = {
                    **_initial_node(nodes[name]),
                    "iteration": state.get("iteration", 0) + int(executed),
                }
        # Old settled descendants need not wait on a parent that just re-entered
        # a loop. Their completion is durable; only unfinished nodes join again.
        graph = {
            name: set() if run["nodes"][name]["status"] in NODE_TERMINAL else parents
            for name, parents in graph.items()
        }
        sorter = TopologicalSorter(graph)
        sorter.prepare()
        ready_work = []
        finish_outcome = None
        while sorter.is_active():
            ready = sorter.get_ready()
            if not ready:
                break
            for name in ready:
                state, node = run["nodes"][name], nodes[name]
                if state["status"] in NODE_TERMINAL:
                    sorter.done(name)
                    continue
                if state["status"] != "pending":
                    continue
                selected = not raw_graph[name] or any(
                    e["to"] == name
                    for parent in raw_graph[name]
                    for e in selected_edges(workflow, run, parent)
                )
                if not selected:
                    state.update(status="skipped", finished_at=now, seq=self._next_seq(run))
                    sorter.done(name)
                elif node["type"] in ("condition", "prompt", "finish"):
                    state.update(
                        status="completed",
                        outcome=node.get("outcome", "success"),
                        finished_at=now,
                        seq=self._next_seq(run),
                    )
                    sorter.done(name)
                    if node["type"] == "finish":
                        finish_outcome = (
                            "failure"
                            if node.get("outcome") == "failure" or finish_outcome == "failure"
                            else "success"
                        )
                elif node["type"] == "human_review":
                    state.update(status="waiting_for_review", started_at=now)
                else:
                    ready_work.append(name)
        statuses = [s["status"] for s in run["nodes"].values()]
        if finish_outcome is not None:
            run["finish_outcome"] = finish_outcome
            self._finish(run, finish_outcome)
            return []
        if ready_work or any(s in ("scheduled", "running") for s in statuses):
            run["status"] = "running"
        elif "waiting_for_review" in statuses:
            run["status"] = "waiting"
        elif self._reentries(run, workflow):
            run["status"] = "running"
        else:
            failed = "pending" in statuses
            for name, state in run["nodes"].items():
                if state["status"] == "skipped":
                    continue
                if (
                    state["status"] == "failed"
                    or nodes[name]["type"] == "finish"
                    and state.get("outcome") == "failure"
                ):
                    failed = True
                if state.get("outcome") == "failure" and not selected_edges(workflow, run, name):
                    failed = True
                if nodes[name]["type"] in ("condition", "human_review") and not selected_edges(
                    workflow, run, name
                ):
                    failed = True
            self._finish(run, "failure" if failed else "success")
        return ready_work

    async def drive(self, run_id: str, owner: str):
        _, workflow, revision = self.db.execution(run_id, owner=owner)
        active: dict[str, asyncio.Task] = {}
        last_heartbeat = time.monotonic()
        try:
            EffectStore(self.db).recover(run_id, owner=owner, revision=revision)
            resuming = []

            def recover(run):
                interrupted = []
                for name, state in run["nodes"].items():
                    if state["status"] in ("running", "scheduled"):
                        if state.get("executor") == "kanban":
                            resuming.append(name)
                        elif state["node_type"] == "wait":
                            state["status"] = "pending"
                        else:
                            state.update(
                                status="failed",
                                outcome="failure",
                                error="worker interrupted by runtime restart; retry explicitly",
                                finished_at=time.time(),
                            )
                            interrupted.append(name)
                if interrupted:
                    run.update(
                        status="failed",
                        error="interrupted nodes: " + ", ".join(interrupted),
                        finished_at=time.time(),
                    )

            recovered = self.db.mutate_run(run_id, recover, owner=owner, revision=revision)
            # A retry can replace the spec immediately after recovery commits
            # a failure. This driver still holds the old spec: finish it using
            # the recovery snapshot so the next driver loads the retry's spec.
            if recovered["status"] in TERMINAL:
                return
            for name in resuming:
                node = next(n for n in workflow["nodes"] if n["id"] == name)
                active[name] = asyncio.create_task(
                    self._execute(
                        run_id,
                        owner,
                        node,
                        workflow,
                        revision,
                        recovered["nodes"][name]["attempt_id"],
                    )
                )
            while True:
                if time.monotonic() - last_heartbeat >= 5:
                    if not self.db.claim(run_id, owner, revision=revision):
                        break
                    last_heartbeat = time.monotonic()
                ready = []
                state = self.db.mutate_run(
                    run_id,
                    lambda r, ready=ready: ready.extend(self._plan(r, workflow)),
                    owner=owner,
                    revision=revision,
                )
                if state["status"] in TERMINAL:
                    break
                limit = settings_module.values(self.db)["max_parallel_steps"]
                for name in ready[: max(0, limit - len(active))]:
                    if name in active:
                        continue

                    def begin(run, name=name):
                        if run["status"] in TERMINAL:
                            return
                        run["nodes"][name].update(status="running", started_at=time.time())
                        raw_node = next(n for n in workflow["nodes"] if n["id"] == name)
                        if on_board(
                            raw_node, run.get("board") or board_for(workflow, run.get("project_id"))
                        ):
                            run["nodes"][name]["executor"] = "kanban"
                            run["nodes"][name]["executor_home"] = str(
                                self.board_executor().host.home
                            )
                            run["nodes"][name]["execution_timeout"] = raw_node.get(
                                "timeout_seconds",
                                settings_module.values(self.db)["default_timeout_seconds"],
                            )
                        if run["nodes"][name]["node_type"] == "wait":
                            run["nodes"][name].setdefault("wait_started_at", time.time())

                    state = self.db.mutate_run(
                        run_id,
                        begin,
                        owner=owner,
                        revision=revision,
                        event={"type": "node_started", "node_id": name},
                    )
                    if state["status"] in TERMINAL:
                        break
                    node = next(n for n in workflow["nodes"] if n["id"] == name)
                    attempt_id = state["nodes"][name]["attempt_id"]
                    active[name] = asyncio.create_task(
                        self._execute(run_id, owner, node, workflow, revision, attempt_id)
                    )
                if state["status"] == "waiting" and not active:
                    break
                if active:
                    done, _ = await asyncio.wait(
                        active.values(), timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                    active = {name: task for name, task in active.items() if not task.done()}
                else:
                    await asyncio.sleep(0.05)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            self.db.release(run_id, owner)

    async def _execute(
        self, run_id: str, owner: str, raw: dict, workflow: dict, revision, attempt_id
    ):
        run, _, _ = self.db.execution(run_id, owner=owner, revision=revision)
        if run["nodes"][raw["id"]].get("attempt_id") != attempt_id:
            raise ConflictError("node attempt was superseded")
        native_card = False
        abort = False
        try:
            node = copy.deepcopy(raw)
            board = run.get("board") or board_for(workflow, run.get("project_id"))
            native_card = on_board(raw, board)
            node.setdefault("profile", workflow.get("defaults", {}).get("profile"))
            if workflow.get("defaults", {}).get("model"):
                node.setdefault("model", workflow["defaults"]["model"])
            if "max_retries" in workflow.get("defaults", {}):
                node.setdefault("max_retries", workflow["defaults"]["max_retries"])
            for key in ("command", "workdir", "branch"):
                if key in node:
                    node[key] = resolve(node[key], run)
            if node.get("wait_for"):
                node["wait_for"] = {k: resolve(v, run) for k, v in node["wait_for"].items()}
            if node["type"] == "agent_task":
                if (
                    node.get("stack") or node.get("workspace", {}).get("type") == "worktree"
                ) and not native_card:
                    raise ValueError("Worktrees require a project-scoped native board node")
                graph = dependencies(workflow)
                lineage = ancestors(graph, node["id"])
                directives = [run["input"]] if run.get("input") else []
                directives += [
                    resolve(n.get("prompt", ""), run)
                    for n in workflow["nodes"]
                    if n["id"] in lineage
                    and n["type"] == "prompt"
                    and run["nodes"][n["id"]]["status"] == "completed"
                ]
                inputs = {
                    key: resolve(value, run) for key, value in node.get("input_mapping", {}).items()
                }
                node["prompt"] = resolve(node["prompt"], run, inputs)
                if directives:
                    node["prompt"] = (
                        "Operator instructions:\n"
                        + "\n\n".join(directives)
                        + "\n\nStep instructions:\n"
                        + node["prompt"]
                    )
                if inputs and any("{{" + key + "}}" not in raw["prompt"] for key in inputs):
                    node["prompt"] += "\n\nInputs:\n" + json.dumps(inputs, ensure_ascii=False)
            config = settings_module.values(self.db)
            retries = node.get("max_retries", workflow.get("defaults", {}).get("max_retries", 0))
            for attempt in range(retries + 1):
                try:
                    timeout = node.get("timeout_seconds", config["default_timeout_seconds"])
                    if native_card:
                        timeout = run["nodes"][node["id"]].get("execution_timeout", timeout)
                    if node["type"] == "wait":
                        timeout -= time.time() - run["nodes"][node["id"]].get(
                            "wait_started_at", time.time()
                        )
                    # Native process executors enforce their own deadline and
                    # need a short window to reap children and report the stage.
                    grace = 2 if node["type"] in ("agent_task", "script") else 0
                    async with asyncio.timeout(max(0.001, timeout + grace)):
                        if native_card:
                            result = await self.board_executor().execute(
                                node,
                                run,
                                board,
                                owner=owner,
                                revision=revision,
                                attempt_id=attempt_id,
                                timeout=timeout,
                            )
                        else:
                            if node["type"] == "agent_task":
                                node["_telemetry_context"] = {
                                    "db": str(self.db.path.resolve()),
                                    "run_id": run_id,
                                    "node_id": raw["id"],
                                    "attempt_id": attempt_id,
                                    "revision": revision,
                                    "stream_key": "direct",
                                }
                            result = await self.executor.execute(node, run, config)
                except AdoptionError:
                    raise
                except Exception as exc:
                    if raw.get("adopt") is True:
                        raise AdoptionError(f"adopt failed: {exc}") from exc
                    result = Result("failure", error=f"{type(exc).__name__}: {exc}")
                # Replaying deterministic shell commands after errors can repeat
                # side effects. Automatic retries apply only to transient agents.
                transient = (
                    node["type"] == "agent_task"
                    and not native_card
                    and any(
                        v in (result.error or "").lower()
                        for v in (
                            "429",
                            "503",
                            "502",
                            "overloaded",
                            "rate limit",
                            "connection reset",
                        )
                    )
                )
                if result.outcome != "failure" or not transient or attempt == retries:
                    break
                run = self.db.mutate_run(
                    run_id,
                    lambda state: state["nodes"][raw["id"]].update(
                        status="running", started_at=time.time()
                    ),
                    owner=owner,
                    revision=revision,
                    attempt=(raw["id"], attempt_id),
                    restart_attempts={
                        raw["id"]: {
                            "outcome": result.outcome,
                            "output": result.output,
                            "error": result.error,
                            "telemetry": result.telemetry,
                        }
                    },
                    event={"type": "node_retry", "node_id": raw["id"]},
                )
                attempt_id = run["nodes"][raw["id"]]["attempt_id"]
                await asyncio.sleep(min(2**attempt, 30))
        except Exception as exc:
            abort = isinstance(exc, AdoptionError) or raw.get("adopt") is True
            result = Result(
                "failure", str(exc) if abort else "", error=f"{type(exc).__name__}: {exc}"
            )

        def settle(state):
            if state["status"] in TERMINAL:
                return
            current = state["nodes"][raw["id"]]
            current.update(
                status="completed",
                outcome=result.outcome,
                output=result.output,
                finished_at=time.time(),
                seq=self._next_seq(state),
            )
            if result.error:
                current["error"] = result.error
            if abort:
                current["abort_run"] = True
            task_ids = extract_task_ids(result.output)
            if task_ids:
                current["task_ids"] = task_ids
            if result.telemetry:
                current["telemetry"] = result.telemetry

        self.db.mutate_run(
            run_id,
            settle,
            owner=owner,
            revision=revision,
            attempt=(raw["id"], attempt_id),
            event={"type": "node_completed", "node_id": raw["id"], "outcome": result.outcome},
        )
        if native_card:
            self.reconcile_external()
