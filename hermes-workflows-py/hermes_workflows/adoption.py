"""Adopt existing cards without replacing, archiving, or unlinking user work.

The workflow DB stores the plan before any mutation. Board-local receipts commit
with assignment/promotion/review, so a lost acknowledgement cannot replay a
handoff after a fast worker completes. The native dispatcher still owns workers.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time

from .db import TERMINAL, ConflictError
from .executor import Result
from .worktrees import Worktrees, release_directive

TASK_REF = re.compile(r"\{\{nodes\.([A-Za-z0-9_-]+)\.output\.task_ids\}\}\Z")
TASK_TOKEN = re.compile(r"\bt_[0-9a-z]+\b")
TASK_BLOCK = re.compile(r"```task_ids\b[^\n]*\n(.*?)```|<task_ids>(.*?)</task_ids>", re.S | re.I)


class AdoptionError(ValueError):
    """An invalid or no-longer-drivable scope must stop the workflow."""


def extract_task_ids(text):
    ids = []
    for match in TASK_BLOCK.finditer(text or ""):
        ids.extend(TASK_TOKEN.findall(match[1] or match[2] or ""))
    return list(dict.fromkeys(ids))


def resolve_task_ref(run, ref):
    match = TASK_REF.fullmatch(ref.strip())
    if not match:
        return [ref.strip()]
    state = run["nodes"].get(match[1], {})
    if state.get("status") != "completed":
        raise AdoptionError("task_ref source has no output in this iteration")
    ids = state.get("task_ids") or extract_task_ids(state.get("output"))
    if not ids:
        ids = TASK_TOKEN.findall(state.get("output") or "")
    if not ids:
        raise AdoptionError("task_ref surfaced no task ids; emit a fenced task_ids block")
    return list(dict.fromkeys(ids))


def ordered_scope(ids, links):
    """Pinned upstream ordering, including its stable cyclic-tail fallback."""
    deps = {identity: set() for identity in ids}
    for parent, child in links:
        if parent in deps and child in deps and parent != child:
            deps[child].add(parent)
    ordered, placed = [], set()
    while len(ordered) < len(ids):
        before = len(ordered)
        for identity in ids:
            if identity not in placed and deps[identity] <= placed:
                ordered.append(identity)
                placed.add(identity)
        if len(ordered) == before:
            ordered.extend(identity for identity in ids if identity not in placed)
            break
    return ordered


class AdoptionExecutor:
    def __init__(self, kanban):
        self.kb = kanban
        self.db = kanban.db
        self.blocked_timeout_seconds = 6 * 60 * 60
        self.worktrees = Worktrees(self.kb.host)

    def plan(self, node, run, board):
        if not board:
            raise AdoptionError("adopt requires a bound Kanban project scope")
        ids = resolve_task_ref(run, node["task_ref"])
        workspace = None
        with self.kb.board(board) as (native, conn, path):
            tasks = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
            missing = [identity for identity in ids if identity not in tasks]
            if missing:
                raise AdoptionError(f"cards do not exist on this board: {missing}")
            links = [
                (r[0], r[1]) for r in conn.execute("SELECT parent_id,child_id FROM task_links")
            ]
            excluded = [
                identity
                for identity in ids
                if tasks[identity]["title"].strip().lower().startswith(("(meta)", "(epic)"))
                and any(
                    p == identity and tasks[c]["status"] not in ("done", "archived")
                    for p, c in links
                )
            ]
            drivable = [identity for identity in ids if identity not in excluded]
            if not drivable:
                raise AdoptionError("scope contains only un-completable umbrella/parent cards")
            internal = [(p, c) for p, c in links if p in drivable and c in drivable]
            if node.get("stack") or node.get("workspace", {}).get("type") == "worktree":
                workspace = self.worktrees.context(node, native, board)
        home = str(self.kb.host.home)
        with self.kb.host.scope():
            canonical = self.kb.host.module("adoption")._canonical_assignee
            assignee = canonical(node["profile"])
            reviewer = canonical(node.get("review_profile"))
        return {
            "home": home,
            "board": board,
            "path": path,
            "key": "adopt:"
            + hashlib.sha256(
                (home + run["run_id"] + run["nodes"][node["id"]]["attempt_id"]).encode()
            ).hexdigest(),
            "ordered": ordered_scope(drivable, internal),
            "excluded": excluded,
            "sequential": bool(node.get("sequential") or internal or node.get("stack"))
            and len(drivable) > 1,
            "workspace": workspace,
            "stack": node.get("stack", False),
            "worktrees": {},
            "barriers": {},
            "assignee": assignee,
            "reviewer": reviewer,
            "done": [],
            "outputs": [],
            "failed": False,
            "driven": [],
            "reviewed": [],
            "blocked_since": None,
        }

    @staticmethod
    def journal(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hermes_workflows_adoptions (action_key TEXT PRIMARY KEY, receipt TEXT NOT NULL)"
        )

    def action(self, plan, identity, stage, args):
        if plan["home"] != str(self.kb.host.home):
            raise AdoptionError("adopted cards belong to a different profile home")
        if stage not in ("adopt", "review") or identity not in plan["ordered"]:
            raise AdoptionError("action is outside the persisted adoption plan")
        # Holding the run's write fence across the native commit makes an
        # action linearize before cancellation/retry, or refuse afterward.
        run_id, owner, revision, node_id, attempt_id = args
        with self.db.connect(write=True) as workflow_conn:
            row = self.db._owned_row(workflow_conn, run_id, owner, revision, (node_id, attempt_id))
            if json.loads(row["state"])["status"] in TERMINAL:
                raise ConflictError("adoption run is terminal")
            with self.kb.board(plan["board"], expected=plan["path"]) as (native, conn, _):
                audit = self.kb.host.module("adoption")._append_event
                with native.write_txn(conn):
                    self.journal(conn)
                    key = f"{plan['key']}:{identity}:{stage}"
                    saved = conn.execute(
                        "SELECT receipt FROM hermes_workflows_adoptions WHERE action_key=?", (key,)
                    ).fetchone()
                    if saved:
                        return json.loads(saved[0])
                    task = conn.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
                    if task is None:
                        raise AdoptionError(f"card {identity} disappeared from the adopted scope")
                    if stage == "review" and task["status"] != "done":
                        return None  # Never reassign a worker in flight.
                    promote = stage == "review" or task["status"] not in (
                        "running",
                        "review",
                        "done",
                        "archived",
                    )
                    if promote:
                        unsatisfied = [
                            r[0]
                            for r in conn.execute(
                                "SELECT p.id FROM tasks p JOIN task_links l ON l.parent_id=p.id WHERE l.child_id=? AND p.status NOT IN ('done','archived')",
                                (identity,),
                            )
                        ]
                        if unsatisfied:
                            raise AdoptionError(
                                f"card {identity} has unfinished parents {unsatisfied}; installed Hermes cannot bypass dependency gates. Complete parents or explicitly unlink them on the board"
                            )
                    receipt = {"status": task["status"]}
                    if stage == "adopt" and plan.get("workspace"):
                        if task["status"] in ("running", "review"):
                            raise AdoptionError(
                                "Cannot re-anchor a card with an active worker or pending review"
                            )
                        workspace = self.worktrees.for_adoption(
                            plan["workspace"],
                            identity,
                            terminal=task["status"] in ("done", "archived"),
                            review=bool(plan["reviewer"]),
                        )
                        body = task["body"] or ""
                        if plan.get("stack") and "[hermes-workflows: release stacking]" not in body:
                            body += release_directive(plan["workspace"]["branch"])
                        if workspace:
                            conn.execute(
                                "UPDATE tasks SET workspace_kind='worktree',workspace_path=?,branch_name=?,body=? WHERE id=?",
                                (workspace["path"], workspace["branch"], body, identity),
                            )
                            receipt["workspace"] = workspace
                    if stage == "review" and identity in plan.get("worktrees", {}):
                        workspace = plan["worktrees"][identity]
                        if (
                            task["workspace_kind"],
                            task["workspace_path"],
                            task["branch_name"],
                        ) != ("worktree", workspace["path"], workspace["branch"]):
                            raise AdoptionError(
                                "Card workspace binding changed before review; reconcile it before retrying"
                            )
                        self.worktrees.verify_workspace(
                            plan["workspace"], plan["worktrees"][identity]
                        )
                    if promote:
                        assignee = plan["reviewer"] if stage == "review" else plan["assignee"]
                        conn.execute(
                            "UPDATE tasks SET assignee=?, consecutive_failures=CASE WHEN assignee IS ? THEN consecutive_failures ELSE 0 END, last_failure_error=CASE WHEN assignee IS ? THEN last_failure_error ELSE NULL END WHERE id=?",
                            (assignee, assignee, assignee, identity),
                        )
                        audit(
                            conn,
                            identity,
                            "assigned",
                            {"assignee": assignee, "from": task["assignee"]},
                        )
                        status = "review" if stage == "review" else "ready"
                        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, identity))
                        if stage == "review":
                            latest = conn.execute(
                                "SELECT id,profile FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                                (identity,),
                            ).fetchone()
                            receipt["baseline_run"] = latest["id"] if latest else 0
                            audit(
                                conn,
                                identity,
                                "review_requested",
                                {
                                    "reviewer": assignee,
                                    "implementer": latest["profile"]
                                    if latest
                                    else task["assignee"],
                                    "workflow_adoption": plan["key"],
                                },
                            )
                        else:
                            audit(
                                conn,
                                identity,
                                "promoted_manual",
                                {"actor": "hermes-workflows-py", "reason": "workflow adoption"},
                            )
                    conn.execute(
                        "INSERT INTO hermes_workflows_adoptions VALUES(?,?)",
                        (key, json.dumps(receipt)),
                    )
                    return receipt

    def barrier(self, plan, identity, args):
        if not plan.get("stack") or identity not in plan.get("worktrees", {}):
            return  # A previously terminal card does not acquire a new worktree.
        intent = plan["barriers"].get(identity)
        if intent and intent["status"] == "completed":
            return
        if intent is None:
            intent = self.worktrees.barrier_intent(plan["workspace"], plan["worktrees"][identity])
            plan["barriers"][identity] = intent
            self.save(plan, args)  # Persist both commit IDs BEFORE Git changes HEAD.
        run_id, owner, revision, node_id, attempt_id = args
        with self.db.connect(write=True) as conn:
            row = self.db._owned_row(conn, run_id, owner, revision, (node_id, attempt_id))
            if json.loads(row["state"])["status"] in TERMINAL:
                raise ConflictError("stacking run is terminal")
            with self.kb.board(plan["board"], expected=plan["path"]) as (native, board_conn, _):
                with native.write_txn(board_conn):
                    task = board_conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (identity,)
                    ).fetchone()
                    workspace = plan["worktrees"][identity]
                    if task is None or (
                        task["workspace_kind"],
                        task["workspace_path"],
                        task["branch_name"],
                    ) != ("worktree", workspace["path"], workspace["branch"]):
                        raise AdoptionError(
                            "Card workspace binding changed before its commit barrier; reconcile it before retrying"
                        )
                    tip = self.worktrees.advance(plan["workspace"], intent)
        plan["workspace"]["tip"] = tip
        intent["status"] = "completed"
        self.save(plan, args)

    def save(self, plan, args):
        run_id, owner, revision, node_id, attempt_id = args

        def change(run):
            node = run["nodes"][node_id]
            node.update(
                adoption=copy.deepcopy(plan),
                driven_task_ids=plan["driven"],
                reviewed_task_ids=plan["reviewed"],
            )
            if plan["driven"]:
                node["hermes_task_id"] = plan["driven"][0]

        self.db.mutate_run(
            run_id, change, owner=owner, revision=revision, attempt=(node_id, attempt_id)
        )

    async def execute(self, node, run, board, *, owner, revision, attempt_id, timeout):
        from .kanban import completion

        args = (run["run_id"], owner, revision, node["id"], attempt_id)
        try:
            current = self.kb._current(*args)
            plan = current["nodes"][node["id"]].get("adoption")
            if plan is None:
                plan = self.plan(node, run, board)
                self.save(plan, args)
            if plan["home"] != str(self.kb.host.home) or plan["board"] != board:
                raise AdoptionError("adopted cards belong to a different profile or board")
            handle = {
                "kind": "kanban_adopt",
                **{k: plan[k] for k in ("home", "board", "path", "key")},
                "task_ids": plan["ordered"],
            }
            self.db.attach_attempt_handle(
                run["run_id"], node["id"], attempt_id, handle, owner=owner, revision=revision
            )
            while True:
                self.kb._current(*args)
                if time.time() - run["nodes"][node["id"]]["started_at"] >= timeout:
                    return Result(
                        "failure",
                        error="adopt timed out while queued, running or awaiting native review",
                    )
                if not plan["driven"]:
                    remaining = [
                        identity for identity in plan["ordered"] if identity not in plan["done"]
                    ]
                    plan["driven"] = remaining[:1] if plan["sequential"] else remaining
                    for identity in plan["driven"]:
                        receipt = self.action(plan, identity, "adopt", args)
                        if receipt.get("workspace"):
                            plan["worktrees"][identity] = receipt["workspace"]
                    self.save(plan, args)
                results, statuses = [], []
                for identity in plan["driven"]:
                    with self.kb.board(board, expected=plan["path"]) as (_, conn, _):
                        task = conn.execute(
                            "SELECT * FROM tasks WHERE id=?", (identity,)
                        ).fetchone()
                        if task is None:
                            raise AdoptionError(f"adopted card {identity} disappeared")
                        latest = conn.execute(
                            "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                            (identity,),
                        ).fetchone()
                        result = completion(task, latest)
                        statuses.append(
                            (identity, task["status"], task["consecutive_failures"] or 0)
                        )
                    if result and result.outcome == "success" and plan["reviewer"]:
                        receipt = self.action(plan, identity, "review", args)
                        if receipt and identity not in plan["reviewed"]:
                            plan["reviewed"].append(identity)
                        # A second native run is required: a receipt alone is
                        # not evidence that the reviewer completed their turn.
                        if not receipt or not latest or latest["id"] <= receipt["baseline_run"]:
                            result = None
                    results.append(result)
                self.save(plan, args)
                if all(result is not None for result in results):
                    plan["failed"] |= any(result.outcome == "failure" for result in results)
                    if plan.get("stack") and plan["failed"]:
                        return Result(
                            "failure",
                            error="Stacked card failed; remaining cards were not promoted",
                        )
                    for identity in plan["driven"]:
                        self.barrier(plan, identity, args)
                    plan["outputs"].extend(result.output for result in results if result.output)
                    plan["done"].extend(plan["driven"])
                    plan["driven"] = []
                    self.save(plan, args)
                    if len(plan["done"]) == len(plan["ordered"]):
                        return Result(
                            "failure" if plan["failed"] else "success", "\n\n".join(plan["outputs"])
                        )
                    continue
                stuck = next(
                    (
                        identity
                        for identity, status, failures in statuses
                        if status in ("ready", "triage", "todo", "review") and failures >= 3
                    ),
                    None,
                )
                if stuck:
                    return Result(
                        "failure",
                        error=f"adopt stuck: card {stuck} has at least 3 consecutive dispatch failures",
                    )
                blocked = any(status == "blocked" for _, status, _ in statuses)
                plan["blocked_since"] = (plan["blocked_since"] or time.time()) if blocked else None
                self.save(plan, args)
                if blocked and time.time() - plan["blocked_since"] >= self.blocked_timeout_seconds:
                    return Result(
                        "failure", error="adopt blocked: driven cards exceeded the blocked time-box"
                    )
                if time.time() - run["nodes"][node["id"]]["started_at"] >= timeout:
                    return Result(
                        "failure",
                        error="adopt timed out while queued, running or awaiting native review",
                    )
                await asyncio.sleep(self.kb.poll_seconds)
        except ConflictError:
            raise
        except Exception as exc:
            raise AdoptionError(f"adopt failed: {exc}") from exc
