"""Safety boundaries for the explicit manual live probe; no SDK or network import."""

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "memory_live_probe", Path(__file__).resolve().parents[1] / "scripts/check_memory_live.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class LiveProbeGuardTests(unittest.TestCase):
    def test_provider_resolved_bank_and_endpoint_are_checked_before_sdk_creation(self):
        target = {"endpoint": "http://127.0.0.1:9696", "bank_id": "hwp-memory-qa-test"}
        created = []

        class Provider:
            def __init__(self):
                self._api_url = target["endpoint"]
                self._write_bank_ids = [target["bank_id"]]

            def record_destination(self):
                return "a" * 64

            def _new_cloud_client(self):
                created.append(True)

        with patch.object(probe, "TARGET", target, create=True):
            provider = probe.guarded_provider(Provider)
            self.assertEqual(provider.record_destination(), "a" * 64)
            for endpoint, banks in (
                (target["endpoint"], ["production"]),
                ("http://127.0.0.1:9697", [target["bank_id"]]),
            ):
                provider._api_url, provider._write_bank_ids = endpoint, banks
                with self.assertRaises(ValueError):
                    provider._new_cloud_client()
            self.assertEqual(created, [])

    def test_unknown_acceptance_counts_toward_budget_and_cannot_be_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            budget = probe.WriteBudget(path, "hwp-memory-qa-test", 2)
            calls = []

            async def ambiguous(**kwargs):
                calls.append(kwargs)
                raise TimeoutError("lost acknowledgement")

            guarded = probe.GuardedClient(SimpleNamespace(aretain_batch=ambiguous), budget)
            for _ in range(2):
                with self.assertRaises((TimeoutError, ValueError)):
                    asyncio.run(
                        guarded.aretain_batch(
                            bank_id="hwp-memory-qa-test",
                            document_id="hermes-record-effect",
                            items=[{}],
                        )
                    )
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ValueError):
                budget.reserve("existing-production-bank", "hermes-record-other")
            budget.reserve("hwp-memory-qa-test", "hermes-record-other")
            with self.assertRaises(ValueError):
                budget.reserve("hwp-memory-qa-test", "hermes-record-third")
            with self.assertRaises(FileExistsError):
                probe.WriteBudget(path, "hwp-memory-qa-test", 2)
            resumed = probe.WriteBudget(path, "hwp-memory-qa-test", 2, resume=True)
            with self.assertRaises(ValueError):
                resumed.reserve("hwp-memory-qa-test", "hermes-record-effect")

    def test_worker_free_fixtures_validate_before_any_service_is_contacted(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
        from hermes_workflows.specs import require_valid

        for review in (False, True):
            require_valid(probe.make_workflow("synthetic-qa", review))

    def test_proposal_rejects_remote_or_mirrored_or_over_budget_targets(self):
        target = {
            "endpoint": "http://127.0.0.1:9696",
            "bank_id": "hwp-memory-qa-test",
            "mirrors": False,
            "additional_banks": [],
            "max_retained_records": 8,
        }
        probe.validate_target(target)
        for changed in (
            {"endpoint": "https://remote.example"},
            {"mirrors": True},
            {"bank_id": "production"},
            {"max_retained_records": 9},
            {"additional_banks": ["production"]},
        ):
            with self.assertRaises(ValueError):
                probe.validate_target({**target, **changed})


if __name__ == "__main__":
    unittest.main()
