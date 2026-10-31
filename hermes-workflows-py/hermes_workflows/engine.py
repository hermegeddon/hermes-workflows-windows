"""Asynchronous DAG execution with persistent review gates and restart recovery."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from graphlib import TopologicalSorter

from . import settings as settings_module
from .db import ACTIVE, NODE_TERMINAL, TERMINAL, ConflictError, Database
from .executor import Executor, Result
from .specs import REVIEWS, dependencies, fill_params, require_valid, resolve


def _initial_node(node: dict) -> dict:
    return {"node_id": node["id"], "node_type": node["type"], "status": "pending"}


def selected_edges(workflow: dict, run: dict, source: str) -> list[dict]:
    if run["nodes"][source]["status"] == "skipped":
        return []
    edges = [e for e in workflow["edges"] if e["from"] == source]
    plain = [e for e in edges if not e.get("condition") and not e.get("fallback")]
    conditioned = [e for e in edges if e.get("condition")]

    def matches(edge):
        condition = edge["condition"]
        if condition["type"] == "review_status":
            return run["nodes"][source].get("review_decision") == condition["equals"]
        return run["nodes"][condition["node"]].get("outcome") == condition["equals"]

    passed = [e for e in conditioned if matches(e)]
    return plain + (passed if passed else [e for e in edges if e.get("fallback")])


class Engine:
    def __init__(self, db: Database, executor: Executor | None = None):
        self.db = db
        self.executor = executor or Executor()

    def start(
        self,
        workflow: dict,
        *,
        workflow_path: str | None = None,
        project_id: str | None = None,
        input: str | None = None,
        params: dict | None = None,
        run_id: str | None = None,
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
        state = {
            "run_id": run_id or f"{workflow['id']}-{uuid.uuid4().hex[:12]}",
            "workflow_id": workflow["id"],
            "workflow_version": workflow["version"],
            "status": "created",
            "started_at": time.time(),
            "finished_at": None,
            "nodes": {n["id"]: _initial_node(n) for n in workflow["nodes"]},
            "params": fill_params(workflow, params if params is not None else {}),
        }
        if workflow_path:
            state["workflow_path"] = workflow_path
        if project_id:
            state["project_id"] = project_id
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

        return self.db.mutate_run(run_id, change, event={"type": "run_cancelled"})

    def review(self, run_id: str, node_id: str, decision: str, note: str | None = None) -> dict:
        if decision not in REVIEWS:
            raise ValueError("decision must be approved, rejected or needs_changes")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string")
        workflow = self.db.get_spec(run_id)
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

        state = self.db.mutate_run(
            run_id, change, event={"type": "review", "node_id": node_id, "decision": decision}
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
                    run["nodes"][node["id"]] = _initial_node(node)
            run.update(status="created", finished_at=None, workflow_version=workflow["version"])
            run.pop("error", None)

        state = self.db.mutate_run(
            run_id, change, event={"type": "run_retried", "node_id": node_id}, spec=workflow
        )
        return state

    @staticmethod
    def _next_seq(run):
        return max((s.get("seq", 0) for s in run["nodes"].values()), default=0) + 1

    def _plan(self, run: dict, workflow: dict) -> list[str]:
        if run["status"] in TERMINAL:
            return []
        now = time.time()
        graph = dependencies(workflow)
        nodes = {n["id"]: n for n in workflow["nodes"]}
        sorter = TopologicalSorter(graph)
        sorter.prepare()
        ready_work = []
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
                selected = not graph[name] or any(
                    e["to"] == name
                    for parent in graph[name]
                    for e in selected_edges(workflow, run, parent)
                )
                if not selected:
                    state.update(status="skipped", finished_at=now)
                    sorter.done(name)
                elif node["type"] in ("condition", "prompt", "finish"):
                    state.update(
                        status="completed",
                        outcome=node.get("outcome", "success"),
                        finished_at=now,
                        seq=self._next_seq(run),
                    )
                    sorter.done(name)
                elif node["type"] == "human_review":
                    state.update(status="waiting_for_review", started_at=now)
                else:
                    ready_work.append(name)
        statuses = [s["status"] for s in run["nodes"].values()]
        if ready_work or any(s in ("scheduled", "running") for s in statuses):
            run["status"] = "running"
        elif "waiting_for_review" in statuses:
            run["status"] = "waiting"
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
            run.update(status="failed" if failed else "completed", finished_at=now)
        return ready_work

    async def drive(self, run_id: str, owner: str):
        workflow = self.db.get_spec(run_id)
        active: dict[str, asyncio.Task] = {}
        last_heartbeat = time.monotonic()
        try:

            def recover(run):
                interrupted = []
                for name, state in run["nodes"].items():
                    if state["status"] in ("running", "scheduled"):
                        if state["node_type"] == "wait":
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

            recovered = self.db.mutate_run(run_id, recover, owner=owner)
            # A retry can replace the spec immediately after recovery commits
            # a failure. This driver still holds the old spec: finish it using
            # the recovery snapshot so the next driver loads the retry's spec.
            if recovered["status"] in TERMINAL:
                return
            while True:
                if time.monotonic() - last_heartbeat >= 5:
                    if not self.db.claim(run_id, owner):
                        break
                    last_heartbeat = time.monotonic()
                ready = []
                state = self.db.mutate_run(
                    run_id,
                    lambda r, ready=ready: ready.extend(self._plan(r, workflow)),
                    owner=owner,
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
                        if run["nodes"][name]["node_type"] == "wait":
                            run["nodes"][name].setdefault("wait_started_at", time.time())

                    state = self.db.mutate_run(
                        run_id, begin, owner=owner, event={"type": "node_started", "node_id": name}
                    )
                    if state["status"] in TERMINAL:
                        break
                    node = next(n for n in workflow["nodes"] if n["id"] == name)
                    active[name] = asyncio.create_task(self._execute(run_id, owner, node, workflow))
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

    async def _execute(self, run_id: str, owner: str, raw: dict, workflow: dict):
        run = self.db.get_run(run_id)
        try:
            node = copy.deepcopy(raw)
            node.setdefault("profile", workflow.get("defaults", {}).get("profile"))
            if workflow.get("defaults", {}).get("model"):
                node.setdefault("model", workflow["defaults"]["model"])
            for key in ("prompt", "command", "workdir"):
                if key in node:
                    node[key] = resolve(node[key], run)
            if node.get("wait_for"):
                node["wait_for"] = {k: resolve(v, run) for k, v in node["wait_for"].items()}
            if node["type"] == "agent_task":
                graph = dependencies(workflow)
                ancestors, pending = set(), list(graph[node["id"]])
                while pending:
                    name = pending.pop()
                    if name not in ancestors:
                        ancestors.add(name)
                        pending.extend(graph[name])
                directives = [run["input"]] if run.get("input") else []
                directives += [
                    resolve(n.get("prompt", ""), run)
                    for n in workflow["nodes"]
                    if n["id"] in ancestors
                    and n["type"] == "prompt"
                    and run["nodes"][n["id"]]["status"] == "completed"
                ]
                inputs = {
                    key: resolve(value, run) for key, value in node.get("input_mapping", {}).items()
                }
                if directives:
                    node["prompt"] = (
                        "Operator instructions:\n"
                        + "\n\n".join(directives)
                        + "\n\nStep instructions:\n"
                        + node["prompt"]
                    )
                if inputs:
                    node["prompt"] += "\n\nInputs:\n" + json.dumps(inputs, ensure_ascii=False)
            config = settings_module.values(self.db)
            retries = node.get("max_retries", workflow.get("defaults", {}).get("max_retries", 0))
            for attempt in range(retries + 1):
                try:
                    timeout = node.get("timeout_seconds", config["default_timeout_seconds"])
                    if node["type"] == "wait":
                        timeout -= time.time() - run["nodes"][node["id"]].get(
                            "wait_started_at", time.time()
                        )
                    # Native process executors enforce their own deadline and
                    # need a short window to reap children and report the stage.
                    grace = 2 if node["type"] in ("agent_task", "script") else 0
                    async with asyncio.timeout(max(0.001, timeout + grace)):
                        result = await self.executor.execute(node, run, config)
                except Exception as exc:
                    result = Result("failure", error=f"{type(exc).__name__}: {exc}")
                # Replaying deterministic shell commands after errors can repeat
                # side effects. Automatic retries apply only to transient agents.
                transient = node["type"] == "agent_task" and any(
                    v in (result.error or "").lower()
                    for v in ("429", "503", "502", "overloaded", "rate limit", "connection reset")
                )
                if result.outcome != "failure" or not transient or attempt == retries:
                    break
                await asyncio.sleep(min(2**attempt, 30))
        except Exception as exc:
            result = Result("failure", error=f"{type(exc).__name__}: {exc}")

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
            if result.telemetry:
                current["telemetry"] = result.telemetry

        self.db.mutate_run(
            run_id,
            settle,
            owner=owner,
            event={"type": "node_completed", "node_id": raw["id"], "outcome": result.outcome},
        )
