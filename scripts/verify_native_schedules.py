"""Verify real profile config/cron and Python trigger execution in temporary homes.

Uses only finish nodes. No provider, shell commands, gateway or delivery service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from verify_native_host import activate


def verify(home):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    import yaml
    from cron import scheduler_script
    from hermes_workflows import settings
    from hermes_workflows.db import Database
    from hermes_workflows.runtime import Runtime
    from hermes_workflows.schedules import Cron
    from hermes_workflows.specs import SpecStore

    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: preserved-model\nplugins:\n  enabled: [other-plugin]\n  other: {value: keep}\n",
        encoding="utf-8",
    )
    db = Database(home / "workflows/runs.db")
    db.save_settings({"trace_enabled": True, "scripts_enabled": False})
    settings.save(db, {"scheduler_backend": "native", "max_parallel_runs": "7"})
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert raw["model"]["default"] == "preserved-model"
    assert raw["plugins"]["enabled"] == ["other-plugin"]
    assert raw["plugins"]["other"] == {"value": "keep"}
    assert "trace_enabled" not in raw["plugins"]["workflows"]
    assert settings.values(db)["trace_enabled"] is True
    assert settings.values(db)["max_parallel_runs"] == 7
    other = home.parent / "another profile"
    other.mkdir()
    other_db = Database(other / "workflows/runs.db")
    assert settings.values(other_db)["max_parallel_runs"] == 4
    (other / ".env").write_text("HERMES_WORKFLOWS_BOARD=other-profile-board\n", encoding="utf-8")
    with patch.dict(os.environ, {"HERMES_WORKFLOWS_BOARD": "launch-profile-board"}):
        assert settings.values(other_db)["internal_board"] == "other-profile-board"
    assert not (other / "config.yaml").exists(), "reads must not materialize migrated settings"
    spring = Cron("30 2 * * *", "America/New_York")
    assert spring.next(datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)) == datetime(
        2026, 3, 9, 6, 30, tzinfo=timezone.utc
    )
    fall = Cron("30 1 * * *", "America/New_York")
    first = fall.next(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc))
    assert fall.next(first) - first == timedelta(hours=1)
    specs = SpecStore([home / "workflows/global"])
    wf = {
        "id": "native-schedule-capture",
        "name": "Native schedule capture",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "cron", "schedule": "*/5 * * * *", "timezone": "America/New_York"},
        "nodes": [{"id": "done", "type": "finish"}],
        "edges": [],
    }
    specs.save({"workflow": wf}, create=True)
    runtime = Runtime(db, specs)
    try:
        runtime.sync_schedule(wf)
        schedule = runtime.schedules.rows()[0]
        identity = schedule["hermes_cron_id"]
        assert schedule["timezone"] == "America/New_York"
        with runtime.schedules.jobs() as jobs:
            assert jobs.get_job(identity)["no_agent"] is True
            assert jobs.get_job(identity)["name"] == "workflow:" + wf["id"]
            assert jobs.get_job(identity)["script"].endswith(".py")
            jobs.pause_job(identity)
        runtime.sync_schedule(wf)
        assert runtime.schedules.rows()[0]["enabled"] is False
        runtime.schedules.action(identity, "resume")
        runtime.schedules.action(identity, "edit", "every 10m")
        runtime.schedules.action(identity, "run")
        with runtime.schedules.jobs() as jobs:
            claimed = jobs.claim_job_for_fire(identity, return_job=True)
            assert isinstance(claimed, dict), claimed
            script = claimed["script"]
            # Native runner chooses the installed Python and applies its usual
            # environment sanitizer, script-root validation and output capture.
            ok, output = scheduler_script._run_job_script(script)
            assert ok, output
            assert len(db.list_runs("all")) == 1, output
            run_id = db.list_runs()[0]["run_id"]
            runtime.engine.cancel(run_id)
            ok, output = scheduler_script._run_job_script(script)
            assert ok and len(db.list_runs("all")) == 1, output
        settings.save(db, {"scheduler_backend": "standalone"})
        runtime.sync_schedule(wf)
        with runtime.schedules.jobs() as jobs:
            assert jobs.get_job(identity)["enabled"] is False
        assert db.schedules()[0]["authority"] == "standalone"
        assert db.schedules()[0]["next_run"] is not None
        runtime.schedules.delete_workflow(wf["id"])
        with runtime.schedules.jobs() as jobs:
            assert jobs.get_job(identity) is None
        assert db.schedules() == []
    finally:
        runtime.close()
    return {
        "profile_config_merge": "passed",
        "profile_isolation": "passed",
        "native_cron_controls": "passed",
        "python_script_runner": "passed",
        "same_claim_replay": "passed",
        "standalone_handoff": "passed",
        "provider_requests": 0,
        "shell_commands": 0,
        "external_messages": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
    with tempfile.TemporaryDirectory(prefix="workflow native schedules żółć ") as temp:
        home = Path(temp).resolve() / "profile"
        os.environ["HERMES_HOME"] = str(home)
        print(json.dumps(verify(home), indent=2))


if __name__ == "__main__":
    main()
