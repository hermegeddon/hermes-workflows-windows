"""Check an already-running supervisor; this process never starts a Runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path


def check(home: Path, timeout: float):
    from hermes_workflows.db import Database
    from hermes_workflows.engine import Engine
    from hermes_workflows.specs import SpecStore

    db = Database(home / "workflows" / "runs.db")
    store = SpecStore([home / "workflows" / "global", home / "workflows" / "templates"])
    engine = Engine(db)
    name = "restart-check-" + uuid.uuid4().hex[:8]
    wf = {
        "id": name,
        "name": "Post-restart verification",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "nodes": [{"id": "review", "type": "human_review"}, {"id": "done", "type": "finish"}],
        "edges": [
            {
                "from": "review",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            }
        ],
    }
    detail = store.save({"workflow": wf}, create=True)
    run = engine.start(wf, workflow_path=detail["path"])
    report = {"workflow_id": name, "run_id": run["run_id"]}

    def wait(run_id, expected):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = db.get_run(run_id)
            if state["status"] == expected:
                return state
            if state["status"] in ("failed", "cancelled"):
                raise RuntimeError(f"unexpected terminal status: {state['status']}")
            time.sleep(0.05)
        raise RuntimeError(
            f"supervisor did not advance {run_id} to {expected}; current status {state['status']}"
        )

    try:
        wait(run["run_id"], "waiting")
        report["automatic_advance"] = "passed"
        approved = time.monotonic()
        engine.review(run["run_id"], "review", "approved", "post-restart verification")
        final = wait(run["run_id"], "completed")
        assert final["nodes"]["done"]["outcome"] == "success"
        report.update(
            review_completion="passed",
            review_latency_ms=round((time.monotonic() - approved) * 1000),
        )
        # Insert a single overdue schedule. Only the persistent service can fire
        # it, because no Runtime/get_runtime is imported or called here.
        cron_name = name + "-cron"
        scheduled = {
            **wf,
            "id": cron_name,
            "name": "Post-restart scheduler verification",
            "nodes": [{"id": "done", "type": "finish"}],
            "edges": [],
            "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        }
        store.save({"workflow": scheduled}, create=True)
        job_id = "workflow-" + cron_name
        try:
            db.put_schedule(
                {
                    "hermes_cron_id": job_id,
                    "workflow_id": cron_name,
                    "cron_expression": "* * * * *",
                    "timezone": "UTC",
                    "enabled": True,
                    "workflow_enabled": True,
                    "last_run": None,
                    "next_run": "2000-01-01T00:00:00+00:00",
                    "spec_signature": json.dumps(scheduled["trigger"], sort_keys=True),
                }
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                rows = db.list_runs("all", cron_name)
                if rows and rows[0]["status"] == "completed":
                    report["automatic_schedule"] = "passed"
                    report["scheduled_run_id"] = rows[0]["run_id"]
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("the persistent scheduler did not fire its due job")
        finally:
            # Keep the test workflow/run history readable, but disable the
            # schedule permanently so later restarts cannot fire it again.
            row = db.schedule(job_id)
            row.update(enabled=False, deleted=True, next_run=None)
            db.put_schedule(row)
            scheduled.update(enabled=False, trigger={"type": "manual"})
            store.save({"workflow": scheduled})
        return report
    finally:
        if db.get_run(run["run_id"])["status"] not in ("completed", "failed", "cancelled"):
            engine.cancel(run["run_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, default=Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    )
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    sys.path.insert(0, str(args.home / "plugins" / "hermes-workflows"))
    print(json.dumps(check(args.home, args.timeout), indent=2))


if __name__ == "__main__":
    main()
