"""Explicitly authorized, single-bank live QA; never imports the installed gateway.

Requires the prepared host/provider sources and a previously reviewed proposal.
The normal test suite imports only the standard-library guard, never a service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class WriteBudget:
    def __init__(self, path, bank_id, limit, *, resume=False):
        self.path, self.bank_id, self.limit = Path(path), bank_id, limit
        self.lock = threading.Lock()
        if resume:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or len(rows) != len(set(rows)) or len(rows) > limit:
                raise ValueError("invalid persisted reservations")
        else:
            with self.path.open("x", encoding="utf-8") as stream:
                json.dump([], stream)

    def reserve(self, bank_id, document_id):
        with self.lock:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if bank_id != self.bank_id or not document_id.startswith("hermes-record-"):
                raise ValueError("destination outside approved QA target")
            if document_id in rows or len(rows) >= self.limit:
                raise ValueError("duplicate/ambiguous send or exhausted QA record budget")
            rows.append(document_id)
            # Reserve before dispatch, including failed/unknown acceptance.
            with self.path.open("w", encoding="utf-8") as stream:
                json.dump(rows, stream)
                stream.flush()
                os.fsync(stream.fileno())


class GuardedClient:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    async def aretain_batch(self, **kwargs):
        if len(kwargs.get("items", [])) != 1:
            raise ValueError("QA accepts exactly one synthetic record per call")
        self.budget.reserve(kwargs["bank_id"], kwargs["document_id"])
        return await self.client.aretain_batch(**kwargs)

    async def aclose(self):
        await self.client.aclose()


def guarded_provider(base):
    """QA-only provider instrumentation; the production consumer stays generic."""

    class GuardedProvider(base):
        def record_destination(self):
            if self._api_url != TARGET["endpoint"] or self._write_bank_ids != [TARGET["bank_id"]]:
                raise ValueError("resolved provider route differs from approved target")
            return super().record_destination()

        def _new_cloud_client(self):
            self.record_destination()
            return GuardedClient(super()._new_cloud_client(), BUDGET)

    return GuardedProvider()


def validate_target(target):
    url = urlsplit(target["endpoint"])
    if (
        url.scheme not in ("http", "https")
        or url.hostname not in ("localhost", "127.0.0.1", "::1")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not target["bank_id"].startswith("hwp-memory-qa-")
        or target.get("mirrors") is not False
        or target.get("additional_banks") != []
        or type(target.get("max_retained_records")) is not int
        or not 1 <= target["max_retained_records"] <= 8
    ):
        raise ValueError("proposal exceeds the approved isolated QA boundaries")


def make_workflow(name, review):
    workflow = {
        "id": name,
        "name": "Synthetic memory QA",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "nodes": [{"id": "done", "type": "finish"}],
        "edges": [],
    }
    if review:
        workflow["nodes"] += [
            {"id": "entry", "type": "prompt", "prompt": "Synthetic entry"},
            {"id": "start", "type": "prompt", "prompt": "Synthetic QA"},
            {"id": "review", "type": "human_review"},
        ]
        workflow["edges"] = [
            {"from": "entry", "to": "start"},
            {"from": "start", "to": "review"},
            {
                "from": "review",
                "to": "start",
                "condition": {"type": "review_status", "equals": "needs_changes"},
            },
            {
                "from": "review",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            },
        ]
    return workflow


def exercise(home, *, review_only=False):
    from hermes_workflows.db import Database
    from hermes_workflows.engine import Engine
    from hermes_workflows.memory_service import MemoryService

    db = Database(home / "workflows/runs.db")
    db.save_settings(
        {
            "workflow_memory_enabled": True,
            "workflow_memory_timeout_seconds": 30,
            "workflow_memory_write_node_failures": False,
        }
    )
    service = MemoryService(db)
    engine = Engine(db, memory_policy=service)
    runs = []
    if review_only:
        first = TARGET["bank_id"] + "-finish"
        assert db.get_run(first)["status"] == "completed"
        first_rows = service.outbox.entries(first)
        assert len(first_rows) == 2 and all(row["status"] == "queued" for row in first_rows)
        assert set(json.loads(BUDGET.path.read_text(encoding="utf-8"))) == {
            "hermes-record-" + row["effect_id"] for row in first_rows
        }
        runs.append({"run_id": first, "status": "completed", "effects": service.records(first)})
    for review in (True,) if review_only else (False, True):
        name = TARGET["bank_id"] + ("-review" if review else "-finish")
        workflow = make_workflow(name, review)
        state = engine.start(workflow, run_id=name)
        if not state.get("memory_export", {}).get("enabled"):
            raise RuntimeError("native writer failed to bind; no workflow dispatch allowed")

        def drive(expected, name=name):
            assert db.claim(name, "live-memory-qa")
            asyncio.run(engine.drive(name, "live-memory-qa"))
            assert db.get_run(name)["status"] == expected

        if review:
            drive("waiting")
            engine.review(name, "review", "needs_changes")
            drive("waiting")
            engine.review(name, "review", "approved")
        drive("completed")
        rows = service.outbox.entries(name)
        assert len(rows) == 2, "only terminal summary and retrospective are authorized here"
        asyncio.run(service.dispatch())
        rows = service.outbox.entries(name)
        assert all(row["status"] in ("queued", "acknowledged") for row in rows), rows
        before = BUDGET.path.read_bytes()
        asyncio.run(service.reconcile(name, rows[0]["effect_id"]))
        assert BUDGET.path.read_bytes() == before, "receipt lookup must never retain"
        runs.append({"run_id": name, "status": "completed", "effects": service.records(name)})
    db.save_settings({"workflow_memory_enabled": False})
    assert not any(
        row["status"] in ("prepared", "in_flight")
        for run in runs
        for row in service.outbox.entries(run["run_id"])
    )
    return runs


async def bank_setup(report, *, existing=False):
    from hindsight_client import Hindsight

    client = Hindsight(base_url=TARGET["endpoint"], timeout=10)
    try:
        if existing:
            if (
                report.get("initial_bank_absent") is not True
                or report.get("bank_created") is not True
                or report["bank_id"] != TARGET["bank_id"]
                or report["marker"] != TARGET["marker"]
            ):
                raise ValueError("continuation does not own the proposed bank")
            configured = await client.aget_bank_config(TARGET["bank_id"])
            if (
                configured["bank_id"] != TARGET["bank_id"]
                or configured["config"]["reflect_mission"]
                != "Synthetic acceptance marker " + TARGET["marker"]
            ):
                raise ValueError("existing bank ownership marker differs")
            return
        found = await client.banks.list_banks(q=TARGET["bank_id"], limit=100, _request_timeout=10.0)
        if found.banks:
            raise RuntimeError("proposed bank query is nonempty; refusing creation/reuse")
        report["initial_bank_absent"] = True
        await client.acreate_bank(
            TARGET["bank_id"],
            name="Disposable workflow memory QA",
            reflect_mission="Synthetic acceptance marker " + TARGET["marker"],
            enable_observations=False,
        )
        report["bank_created"] = True
        await client.aupdate_bank_config(
            TARGET["bank_id"], store_document_text=True, enable_auto_consolidation=False
        )
    finally:
        await client.aclose()


async def readback(runs):
    from hindsight_client import Hindsight
    from hindsight_client_api.exceptions import NotFoundException

    client = Hindsight(base_url=TARGET["endpoint"], timeout=10)
    pending = {
        "hermes-record-" + row["effect_id"]: row["record"] for run in runs for row in run["effects"]
    }
    confirmed = []
    deadline = time.monotonic() + 180
    try:
        while pending and time.monotonic() < deadline:
            for document_id, record in list(pending.items()):
                try:
                    document = await client.documents.get_document(
                        TARGET["bank_id"], document_id, _request_timeout=10.0
                    )
                except NotFoundException:
                    continue
                if document.original_text and document.memory_unit_count > 0:
                    assert document.bank_id == TARGET["bank_id"]
                    assert json.loads(document.original_text) == record
                    confirmed.append(
                        {
                            "document_id": document_id,
                            "memory_unit_count": document.memory_unit_count,
                            "original_record_matches": True,
                        }
                    )
                    del pending[document_id]
            if pending:
                await asyncio.sleep(2)
        if pending:
            raise TimeoutError("readback incomplete; preserve receipts and do not resend")
        listed = await client.documents.list_documents(
            TARGET["bank_id"], limit=100, _request_timeout=10.0
        )
        assert listed.total == len(confirmed)
        return confirmed
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--provider-source", type=Path, required=True)
    parser.add_argument("--sdk-site", type=Path, required=True)
    parser.add_argument("--allow-live", action="store_true", required=True)
    parser.add_argument(
        "--continue-review",
        action="store_true",
        help="Continue only the unstarted review fixture; never resend prior effects",
    )
    args = parser.parse_args()
    global TARGET, BUDGET
    TARGET = json.loads(args.proposal.read_text(encoding="utf-8"))
    validate_target(TARGET)
    home = Path(TARGET["profile_home"]).resolve()
    if home.exists() and not args.continue_review:
        raise RuntimeError("QA profile already exists; refusing rerun after ambiguous writes")
    if not args.continue_review:
        home.mkdir(parents=True)
    BUDGET = WriteBudget(
        home.parent / "write-reservations.json",
        TARGET["bank_id"],
        TARGET["max_retained_records"],
        resume=args.continue_review,
    )
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(args.hermes_source.resolve()), str(root / "hermes-workflows-py")]
    from hermes_workflows.specs import require_valid

    for review in (False, True):
        require_valid(make_workflow(TARGET["bank_id"] + "-validate", review))
    sys.path.append(str(args.sdk_site.resolve()))
    sys.modules["check_memory_live"] = sys.modules[__name__]
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_RUNTIME_DIR"] = str(root / ".cache/hermes-memory-buildhome/tools")
    (home / "config.yaml").write_text(
        json.dumps(
            {
                "memory": {"provider": "hindsight"},
                "terminal": {"cwd": str(home)},
                "plugins": {
                    "isolation": "in_process",
                    "entries": {
                        "hermes-workflows": {"granted_capabilities": ["memory.write_records"]}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config = {
        "mode": "local_external",
        "api_url": TARGET["endpoint"],
        "bank_id": TARGET["bank_id"],
        "bank_id_template": "",
        "trusted_project_dirs": [],
        "mirror_to_own_bank": False,
        "additional_banks": [],
        "retain_async": True,
        "auto_retain": False,
        "auto_recall": False,
        "retain_source": TARGET["marker"],
    }
    (home / "hindsight").mkdir(exist_ok=True)
    (home / "hindsight/config.json").write_text(json.dumps(config), encoding="utf-8")
    provider = home / "plugins/hindsight"
    provider.mkdir(parents=True, exist_ok=True)
    for path in args.provider_source.glob("*.py"):
        if path.name != "setup.py":
            shutil.copyfile(path, provider / path.name)
    with (provider / "__init__.py").open("a", encoding="utf-8") as stream:
        stream.write(
            "\nfrom check_memory_live import guarded_provider\n"
            "def register(ctx): ctx.register_memory_provider(guarded_provider(HindsightMemoryProvider))\n"
        )
    (provider / "plugin.yaml").write_text("name: hindsight\nversion: 1.2.3\n", encoding="utf-8")
    report = {
        "bank_id": TARGET["bank_id"],
        "endpoint": TARGET["endpoint"],
        "marker": TARGET["marker"],
        "gateway_update": False,
        "bank_deleted": False,
    }
    report_path = home.parent / "acceptance.json"
    if args.continue_review:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.setdefault("prior_failures", []).append(report.pop("failure_type", None))
    try:
        asyncio.run(bank_setup(report, existing=args.continue_review))
        report["runs"] = exercise(home, review_only=args.continue_review)
        report["documents"] = asyncio.run(readback(report["runs"]))
        report["readback_passed"] = True
    except BaseException as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report["attempted_documents"] = json.loads(BUDGET.path.read_text(encoding="utf-8"))
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "runs"}, indent=2))


if __name__ == "__main__":
    main()
