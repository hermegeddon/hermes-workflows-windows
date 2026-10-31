from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.agent_worker import run_agent


class AgentResultTests(unittest.TestCase):
    def run_result(self, result):
        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        config = types.ModuleType("hermes_cli.config")
        config.load_config = lambda: {"model": {"default": "test"}}
        package.config = config
        profiles = types.ModuleType("hermes_cli.profiles")
        profiles.resolve_profile_env = lambda name: "C:/fake-home"
        environment = types.ModuleType("hermes_cli.env_loader")
        environment.load_hermes_dotenv = lambda **kwargs: None
        provider = types.ModuleType("hermes_cli.runtime_provider")
        provider.resolve_runtime_provider = lambda **kwargs: {}
        runner = types.ModuleType("run_agent")

        class Agent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, prompt):
                return result

            def close(self):
                pass

        runner.AIAgent = Agent
        modules = {
            "hermes_cli": package,
            "hermes_cli.config": config,
            "hermes_cli.profiles": profiles,
            "hermes_cli.env_loader": environment,
            "hermes_cli.runtime_provider": provider,
            "run_agent": runner,
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ):
            return run_agent({"profile": "default", "prompt": "test"})

    def test_incomplete_result_is_failure_even_with_text(self):
        result = self.run_result({"completed": False, "final_response": "interrupted"})
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(result["error"], "interrupted")

    def test_output_cannot_override_execution_failure(self):
        result = self.run_result(
            {"completed": False, "final_response": '{"node_outcome":"success"}'}
        )
        self.assertEqual(result["outcome"], "failure")

    def test_completed_result_can_report_domain_failure(self):
        result = self.run_result(
            {"completed": True, "final_response": '{"node_outcome":"failure"}', "api_calls": 1}
        )
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(result["telemetry"]["api_calls"], 1)


if __name__ == "__main__":
    unittest.main()
