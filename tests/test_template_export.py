from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-workflows-py"))
from hermes_workflows.api import API
from hermes_workflows.cli import main
from hermes_workflows.db import Database
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, parse_document, require_valid
from hermes_workflows.template_export import (
    build_bundle,
    cache_key,
    clean_hints,
    debind,
    default_model,
    export,
    generate_hints,
    generation_request,
    inventory,
    spec_sha,
    write_bundle,
)

REFERENCE = json.loads((ROOT / "tests/fixtures/template-reference.json").read_text(encoding="utf-8"))
AT = "2026-11-01T12:00:00+00:00"


def source():
    return copy.deepcopy(REFERENCE["cases"][0]["workflow"])


class TemplateExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="template żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = Database(self.home / "workflows/runs.db")

    def test_frozen_pinned_structured_bindings_inventory_and_hint_request(self):
        for case in REFERENCE["cases"]:
            with self.subTest(case["name"]):
                wf = copy.deepcopy(case["workflow"])
                before = copy.deepcopy(wf)
                expected = case["expected"]
                self.assertEqual(debind(wf, case["hints"]), (
                    expected["debound"]["workflow"], expected["debound"]["placeholders"]))
                self.assertEqual(inventory(wf), expected["inventory"])
                self.assertEqual(generation_request(wf)["nodes"], expected["nodes"])
                self.assertEqual(wf, before)

    def test_artifact_round_trip_typed_params_provenance_and_prerequisites(self):
        wf = source()
        bundle = build_bundle(wf, generated_at=AT)
        document = parse_document(bundle["yaml"])
        self.assertEqual(document["template"], bundle["version"])
        self.assertEqual(document["params"], wf["params"])
        self.assertEqual(document["nodes"][0]["prompt"], wf["nodes"][0]["prompt"])
        self.assertEqual(document["scope"]["projects"], ["${PROJECT}", "${PROJECT_2}", "${PROJECT}"])
        self.assertEqual(document["nodes"][2]["task_ref"], "{{nodes.plan.output.task_ids}}")
        self.assertEqual(document["nodes"][0]["skills"], ["architecture"])
        self.assertNotIn("source-model", bundle["yaml"])
        self.assertNotIn("native-worker", bundle["yaml"])
        self.assertNotIn("discord:123:456", bundle["yaml"])
        self.assertLess(bundle["md"].index("## Prerequisites"), bundle["md"].index("## Workflow-level placeholders"))
        self.assertIn("REQUIRED", bundle["md"])
        self.assertIn("RECOMMENDED", bundle["md"])
        self.assertIn("architecture", bundle["md"])
        for placeholder in bundle["placeholders"]:
            self.assertIn(placeholder["token"], bundle["md"])
        self.assertEqual(bundle["version"]["source"], {
            "workflow_id": wf["id"], "workflow_version": 3, "spec_sha": spec_sha(wf)})
        self.assertEqual(bundle["version"]["generation"]["model"], None)
        self.assertEqual(bundle, build_bundle(wf, generated_at=AT))

    def test_adapted_template_imports_without_yaml_dependency(self):
        document = json.loads(build_bundle(source(), generated_at=AT)["yaml"])
        document.pop("template")
        document["id"] = "adapted"
        document["scope"]["projects"] = ["target-project"]
        document["defaults"].update(profile="target-worker", model="target-model")
        document["deliver"] = "local"
        for node in document["nodes"]:
            for key in ("profile", "model", "workdir", "task_ref"):
                if isinstance(node.get(key), str) and node[key].startswith("${"):
                    node[key] = {"profile": "worker", "model": "model", "workdir": str(self.home),
                                 "task_ref": "t_target"}[key]
        require_valid(document)
        store = SpecStore([self.home / "workflows/global"])
        result = store.save({"workflow": document}, create=True)
        self.assertEqual(store.get("adapted")["workflow"], result["workflow"])
        with patch.dict(sys.modules, {"yaml": None}):
            self.assertEqual(parse_document(json.dumps(document)), document)

    def test_windows_bindings_inventory_and_portable_references(self):
        wf = source()
        wf["nodes"][0]["prompt"] = r'Use "C:\Work Space\żółć" and `\\server\share`. Keep {{params.count}}.'
        wf["nodes"][1].update(workdir=r"C:\QA\repo", review_profile="local-reviewer")
        wf["nodes"][2]["workdir"] = "{{nodes.plan.output.path}}"
        wf["nodes"].insert(3, {"id": "script", "type": "script", "shell": "cmd",
                               "command": "echo {{params.count}}", "workdir": r"D:\Scripts"})
        wf["edges"][-1] = {"from": "follow", "to": "script"}
        wf["edges"].append({"from": "script", "to": "done"})
        bundle = build_bundle(wf, generated_at=AT)
        doc = json.loads(bundle["yaml"])
        self.assertEqual(doc["nodes"][0]["prompt"], wf["nodes"][0]["prompt"])
        self.assertEqual(doc["nodes"][1]["workdir"], "${WORKDIR}")
        self.assertTrue(doc["nodes"][1]["review_profile"].startswith("${PROFILE:"))
        self.assertEqual(doc["nodes"][2]["workdir"], "{{nodes.plan.output.path}}")
        self.assertEqual(doc["nodes"][3]["workdir"], "${WORKDIR}")
        self.assertIn(r"C:\Work Space\żółć", [item["match"] for item in bundle["inventory"]])
        self.assertIn(r"\\server\share", [item["match"] for item in bundle["inventory"]])
        self.assertTrue(any(item["kind"] == "path" for item in bundle["inventory"]))

    def test_cosmetic_order_ui_and_previous_provenance_do_not_invalidate_cache(self):
        wf = source()
        first = export(wf, self.db, generated_at=AT)
        changed = {k: copy.deepcopy(wf[k]) for k in reversed(wf)}
        changed["defaults"] = dict(reversed(list(changed["defaults"].items())))
        changed.update(ui={"nodes": {"plan": {"x": 123}}}, template={"old": "provenance"})
        second = export(changed, self.db, generated_at="ignored")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second, {**first, "cached": True})
        self.assertNotIn('"ui"', first["yaml"])

    def test_every_identity_component_invalidates_and_tuple_has_no_delimiter_collisions(self):
        wf = source()
        legacy = {"yaml": "old snapshot", "md": "old generic guide"}
        self.db.export_cache("offline-template-v1:legacy-sha", legacy)
        first = export(wf, self.db, generated_at=AT)
        self.assertFalse(first["cached"])
        self.assertEqual(self.db.export_cache("offline-template-v1:legacy-sha"), legacy)
        edited = copy.deepcopy(wf)
        edited["nodes"][0]["prompt"] += " Carefully."
        for candidate in (edited, {**wf, "id": "other-id"}, {**wf, "version": 4}):
            result = export(candidate, self.db, generated_at=AT)
            self.assertFalse(result["cached"])
            self.assertNotEqual(first["revision"], result["revision"])
        self.assertNotEqual(cache_key(wf), cache_key(wf, "none"))
        self.assertNotEqual(cache_key(wf), cache_key(wf, template_format=2))
        regenerated = export(wf, self.db, generator_version=2, generated_at=AT)
        self.assertNotEqual(first["revision"], regenerated["revision"])
        self.assertNotEqual(cache_key(wf, "a|b"), cache_key(wf, "a"))
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                cache_key(wf, generator_version=invalid)

    def test_offline_export_never_resolves_model_or_calls_provider(self):
        with patch("hermes_workflows.template_export.default_model", side_effect=AssertionError("model lookup")), \
             patch("hermes_workflows.template_export.generate_hints", side_effect=AssertionError("provider call")):
            result = export(source(), self.db)
            self.assertIn("Prerequisites", result["md"])
        with self.assertRaises(ValueError):
            export(source(), self.db, model="m")

    def test_opt_in_hints_cache_and_model_invalidation(self):
        calls = []
        def hints(model, request, **kwargs):
            calls.append((model, request, kwargs))
            return {"overview": "Build a feature.", "nodes": [{"nodeId": "plan", "role": "expert planner"}]}
        first = export(source(), self.db, with_hints=True, model="m@provider", hint_runner=hints)
        second = export(source(), self.db, with_hints=True, model="m@provider", hint_runner=hints)
        third = export(source(), self.db, with_hints=True, model="different@provider", hint_runner=hints)
        self.assertEqual(len(calls), 2)
        self.assertTrue(second["cached"])
        self.assertNotEqual(first["revision"], third["revision"])
        self.assertIn("expert planner", first["yaml"])
        self.assertEqual(calls[0][2]["home"], self.home)
        self.assertEqual(first["version"]["generation"]["model"], "m@provider")

    def test_default_model_reads_only_the_explicit_export_home(self):
        with patch("hermes_workflows.template_export.NativeHost") as host:
            host.return_value.call.return_value = {"model": {"default": "selected", "provider": "fixture"}}
            self.assertEqual(default_model(self.home), "selected@fixture")
            host.assert_called_once_with(self.home)
            host.return_value.call.assert_called_once_with("config", "load_config")
            host.return_value.call.return_value = {"model": {"default": "selected@other", "provider": "fixture"}}
            self.assertEqual(default_model(self.home), "selected@other")
            host.return_value.call.side_effect = OSError("unavailable config")
            self.assertIsNone(default_model(self.home))

    def test_hint_failures_and_malformed_fields_fall_back_and_cache(self):
        for index, value in enumerate((None, [], "bad", {"nodes": [{"nodeId": []}]}, TimeoutError())):
            def fail(*args, value=value, **kwargs):
                if isinstance(value, Exception):
                    raise value
                return value
            result = export(source(), self.db, with_hints=True, model=f"m{index}", hint_runner=fail)
            self.assertIn("Prerequisites", result["md"])
            self.assertEqual(result["version"]["generation"]["hints"], "offline_fallback")
            self.assertTrue(export(source(), self.db, with_hints=True, model=f"m{index}",
                                   hint_runner=lambda *a, **k: self.fail("cached hints retried"))["cached"])
        with patch("hermes_workflows.template_export.default_model", return_value=None):
            self.assertIsNone(export(source(), self.db, with_hints=True)["version"]["generation"]["model"])
        cleaned = clean_hints({"nodes": [{"nodeId": "unknown", "role": "bad"},
                                         {"nodeId": "plan", "role": "${bad}\n`injection`"}]}, source())
        self.assertNotIn("{", cleaned["nodes"][0]["role"])
        for timeout in (0, -1, 121, float("nan"), True):
            with self.assertRaises(ValueError):
                export(source(), self.db, with_hints=True, hint_timeout=timeout)
        for model in ("", " ", 0):
            with self.assertRaises(ValueError):
                export(source(), self.db, with_hints=True, model=model)

    def test_concurrent_exports_converge_on_the_same_persisted_artifacts(self):
        def build(index):
            return export(source(), self.db, generated_at=f"timestamp-{index}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(build, range(8)))
        self.assertEqual(sum(not item["cached"] for item in results), 1)
        self.assertEqual(len({item["yaml"] for item in results}), 1)
        self.assertEqual(len({item["md"] for item in results}), 1)

    def test_http_contract_is_offline_and_readonly_to_workflow_and_settings(self):
        store = SpecStore([self.home / "workflows/global"])
        original = store.save({"workflow": source()}, create=True)
        path = Path(original["path"])
        raw = path.read_bytes()
        runtime = Runtime(self.db, store)
        self.addCleanup(runtime.close)
        with patch.object(runtime, "start", side_effect=AssertionError("export starts workers")):
            api = API(runtime)
            status, bundle = api.handle("GET", "/workflows/portable-qa/export-template")
            self.assertEqual(status, 200)
            self.assertEqual(api.handle("GET", "/workflows/portable-qa/export-template")[1],
                             {**bundle, "cached": True})
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.db.settings(), {})
        self.assertFalse(self.db.list_runs("all"))

    def test_cli_materializes_cached_unicode_artifacts_without_starting_runtime(self):
        store = SpecStore([self.home / "workflows/global"])
        store.save({"workflow": source()}, create=True)
        runtime = Runtime(self.db, store)
        output = self.home / "bundle żółć"
        printed = io.StringIO()
        with patch("hermes_workflows.cli.Runtime", return_value=runtime), \
             patch.object(runtime, "start", side_effect=AssertionError("export starts workers")), \
             redirect_stdout(printed):
            self.assertEqual(main(["export", "portable-qa", "--as-template", "--out-dir", str(output)]), 0)
        first = json.loads(printed.getvalue())
        timestamps = {}
        for kind in ("yaml", "md"):
            self.assertEqual(Path(first["files"][kind]).read_bytes(), first[kind].encode("utf-8"))
            timestamps[kind] = Path(first["files"][kind]).stat().st_mtime_ns
        cached = export(source(), self.db)
        repaired = write_bundle(cached, output)
        self.assertEqual(repaired["yaml"], first["yaml"])
        self.assertEqual({k: Path(first["files"][k]).stat().st_mtime_ns for k in timestamps}, timestamps)
        Path(first["files"]["md"]).unlink()
        write_bundle(cached, output)
        self.assertEqual(Path(first["files"]["md"]).read_bytes(), first["md"].encode("utf-8"))
        self.assertFalse(list(output.glob("*.tmp")))
        with self.assertRaises(ValueError):
            write_bundle({**cached, "yaml_filename": "../escape"}, output)

    def test_cli_subprocess_writes_template_with_stdlib_and_no_hermes_dependency(self):
        store = SpecStore([self.home / "workflows/global"])
        store.save({"workflow": source()}, create=True)
        env = dict(os.environ, HERMES_HOME=str(self.home), PYTHONPATH=str(ROOT / "hermes-workflows-py"),
                   PYTHONIOENCODING="utf-8")
        process = subprocess.run([sys.executable, "-S", "-m", "hermes_workflows", "export", "portable-qa",
                                  "--as-template", "--out-dir", str(self.home / "output")],
                                 env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["id"], "portable-qa")
        self.assertFalse(self.db.list_runs("all"))

    def test_artifact_replace_failure_keeps_original_and_cleans_temporary_file(self):
        bundle = export(source(), self.db)
        output = self.home / "locked-output"
        output.mkdir()
        existing = output / bundle["yaml_filename"]
        existing.write_text("original", encoding="utf-8")
        with patch.object(Path, "replace", side_effect=PermissionError("fixture locked file")):
            with self.assertRaises(PermissionError):
                write_bundle(bundle, output)
        self.assertEqual(existing.read_text(encoding="utf-8"), "original")
        self.assertFalse(list(output.glob("*.tmp")))

    def test_real_hint_worker_uses_scoped_files_without_agent_tools(self):
        fake = self.home / "fake-modules"
        for package in ("hermes_cli", "agent"):
            (fake / package).mkdir(parents=True)
            (fake / package / "__init__.py").write_text("", encoding="utf-8")
        (fake / "hermes_cli/env_loader.py").write_text("def load_hermes_dotenv(): pass\n", encoding="utf-8")
        adapter = fake / "agent/auxiliary_client.py"
        adapter.write_text(
            "import json,os\nfrom pathlib import Path\nfrom types import SimpleNamespace as S\n"
            "def create(**kwargs):\n"
            " assert 'tools' not in kwargs\n"
            " Path(os.environ['HERMES_HOME'],'hint-call.json').write_text(json.dumps(kwargs),encoding='utf-8')\n"
            " return S(choices=[S(message=S(content='```json\\n{\"overview\":\"worker advice\"}\\n```'))])\n"
            "def resolve_provider_client(provider,model):\n"
            " assert (provider,model)==('fixture','hint-model')\n"
            " return S(chat=S(completions=S(create=create))),model\n", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(fake)}):
            hints = generate_hints("hint-model@fixture", generation_request(source()), home=self.home, timeout=10)
        self.assertEqual(hints, {"overview": "worker advice"})
        call = json.loads((self.home / "hint-call.json").read_text(encoding="utf-8"))
        self.assertEqual(call["max_tokens"], 2048)
        adapter.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(fake)}):
            self.assertIsNone(generate_hints("hint-model@fixture", {}, home=self.home, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
