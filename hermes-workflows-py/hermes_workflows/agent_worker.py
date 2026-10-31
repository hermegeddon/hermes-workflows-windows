"""Isolated Python Hermes agent worker; profile/cwd never mutate the host.

Requests and results are UTF-8 files. Hermes' stdout logging cannot corrupt the
result, and the parent can cancel the entire Windows job, including tool children.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sys
import time
from pathlib import Path


def run_agent(node: dict, progress=lambda stage: None, *, preflight=False) -> dict:
    progress("selecting_profile")
    profile = node.get("profile")
    # Hermes supports both named home profiles and agent-roster profiles. Home
    # selection must occur before importing config/runtime/agent modules.
    from hermes_cli.profiles import resolve_profile_env

    roster = {}
    if profile:
        try:
            profile_home = resolve_profile_env(profile)
        except (FileNotFoundError, ValueError):
            import yaml

            from .db import hermes_home

            path = hermes_home() / "agent-roster" / "agents.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            roster = (data or {}).get("agents", {}).get(profile)
            if not isinstance(roster, dict):
                raise ValueError(f"unknown Hermes profile: {profile}") from None
        else:
            os.environ["HERMES_HOME"] = profile_home
        os.environ["HERMES_PROFILE"] = profile

    # Only this isolated worker loads the selected profile's environment. The
    # gateway's shared os.environ must never be switched between profiles.
    progress("loading_profile_environment")
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.env_loader":
            raise
    else:
        if node.get("load_external_secrets") is False:
            # run_agent also calls this public loader during import. Keep the
            # explicit per-node opt-out consistent throughout this isolated
            # process; managed env policy and profile dotenv still load.
            original_load = load_hermes_dotenv

            def load_worker_environment(*args, **kwargs):
                kwargs["load_external_secrets"] = False
                return original_load(*args, **kwargs)

            sys.modules["hermes_cli.env_loader"].load_hermes_dotenv = load_worker_environment
            load_hermes_dotenv = load_worker_environment
        load_hermes_dotenv(
            hermes_home=os.environ.get("HERMES_HOME"),
            load_external_secrets=node.get("load_external_secrets", True),
        )

    progress("importing_agent_modules")
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider

    config = load_config()
    # Newer Hermes exposes a parse guard; fail before provider resolution if a
    # corrupt config would otherwise silently select its fallback defaults.
    import hermes_cli.config as config_module

    guard = getattr(config_module, "require_parseable_user_config", None)
    if callable(guard):
        guard()
    model_config = config.get("model", {})
    if isinstance(model_config, str):
        model_config = {"default": model_config}
    model = node.get("model") or roster.get("model") or model_config.get("default", "")
    model, separator, provider = model.partition("@")
    progress("resolving_provider")
    runtime = resolve_runtime_provider(
        requested=provider if separator else model_config.get("provider"),
        target_model=model or None,
    )
    if preflight:
        return {
            "outcome": "success",
            "output": "WORKFLOW_PREFLIGHT_OK",
            "error": None,
            "telemetry": {},
        }
    progress("importing_agent_modules")
    from run_agent import AIAgent

    kwargs = {
        "model": model,
        "quiet_mode": True,
        "platform": "workflow",
        "skip_background_review": True,
        "load_soul_identity": True,
        "skip_context_files": not bool(node.get("workdir")),
        **{
            k: runtime[k]
            for k in (
                "api_key",
                "base_url",
                "provider",
                "api_mode",
                "requested_provider",
                "request_overrides",
            )
            if k in runtime
        },
    }
    if runtime.get("command"):
        kwargs.update(acp_command=runtime["command"], acp_args=runtime.get("args"))
    if node.get("workdir"):
        kwargs["cwd"] = str(Path(node["workdir"]).expanduser().resolve())
        os.environ["TERMINAL_CWD"] = kwargs["cwd"]
    if roster.get("system_prompt"):
        kwargs["ephemeral_system_prompt"] = roster["system_prompt"]
    for key in ("enabled_toolsets", "disabled_toolsets", "max_iterations"):
        if key in roster:
            kwargs[key] = roster[key]
    for key in ("enabled_toolsets", "disabled_toolsets", "max_iterations", "max_tokens"):
        if key in node:
            kwargs[key] = node[key]
    if "max_iterations" not in kwargs:
        limit = config.get("agent", {}).get("max_turns", config.get("max_turns"))
        resolve_limit = getattr(config_module, "resolve_turn_limit", None)
        if callable(resolve_limit):
            kwargs["max_iterations"] = resolve_limit(limit)
    # Keep compatibility with earlier Hermes constructors while supplying all
    # supported provider fields on current versions.
    signature = inspect.signature(AIAgent)
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
    progress("constructing_agent")
    agent = AIAgent(**kwargs)
    started = time.monotonic()
    try:
        prompt = node["prompt"]
        for skill in node.get("skills", []):
            module = importlib.import_module("tools.skills_tool")
            loader = module.skill_view
            content = loader(skill)
            prompt += f"\n\nSkill {skill}:\n{content}"
        progress("running_conversation")
        result = agent.run_conversation(prompt)
        if not isinstance(result, dict):
            raise RuntimeError("Hermes run_conversation returned an invalid result")
        error = result.get("error")
        output = result.get("final_response") or result.get("response") or ""
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        incomplete = result.get("completed") is False or result.get("success") is False
        outcome = "failure" if error or result.get("failed") or incomplete else "success"
        if outcome == "failure" and not error:
            error = output or "Hermes agent did not complete its response"
        # Hermes workers can explicitly report a domain outcome.
        try:
            structured = json.loads(output)
            if (
                outcome == "success"
                and isinstance(structured, dict)
                and structured.get("node_outcome")
                in (
                    "success",
                    "failure",
                )
            ):
                outcome = structured["node_outcome"]
        except ValueError:
            pass
        telemetry = {"duration_ms": round((time.monotonic() - started) * 1000)}
        if result.get("turn_exit_reason"):
            telemetry["turn_exit_reason"] = str(result["turn_exit_reason"])
        if isinstance(result.get("api_calls"), int):
            telemetry["api_calls"] = result["api_calls"]
        for target, sources in {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
            "total_tokens": ("total_tokens",),
        }.items():
            for source in sources:
                if type(result.get(source)) in (int, float):
                    telemetry[target] = result[source]
                    break
        return {
            "outcome": outcome,
            "output": output,
            "error": str(error) if error else None,
            "telemetry": telemetry,
        }
    finally:
        progress("closing_agent")
        close = getattr(agent, "close", None)
        if callable(close):
            close()


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    def progress(stage):
        if request.get("progress"):
            path = Path(request["progress"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"stage": stage}), encoding="utf-8")
            temporary.replace(path)

    try:
        result = run_agent(request["node"], progress, preflight=request.get("preflight", False))
    except Exception as exc:
        result = {
            "outcome": "failure",
            "output": "",
            "error": f"{type(exc).__name__}: {exc}",
            "telemetry": {},
        }
    path = Path(request["response"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
