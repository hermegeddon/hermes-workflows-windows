"""Bounded, synthetic process/worker timing. No gateway, model or memory calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-workflows-py"))


def stub_worker(directory):
    fake = directory / "synthetic-host"
    package = fake / "hermes_cli"
    package.mkdir(parents=True)
    for name in ("__init__", "profiles"):
        (package / (name + ".py")).write_text(
            "def resolve_profile_env(name): return " + repr(str(directory)) + "\n"
            if name == "profiles"
            else "",
            encoding="utf-8",
        )
    (package / "config.py").write_text(
        "def load_config(): return {'model':{'default':'synthetic','provider':'synthetic'}}\n",
        encoding="utf-8",
    )
    (package / "runtime_provider.py").write_text(
        "def resolve_runtime_provider(**kw): return {'provider':'synthetic'}\n", encoding="utf-8"
    )
    (fake / "run_agent.py").write_text(
        "import json,time\nclass AIAgent:\n"
        " def __init__(self,**kw): pass\n"
        " def run_conversation(self,prompt): return {'final_response':json.dumps({'first_agent_code_ns':time.perf_counter_ns()})}\n"
        " def close(self): pass\n",
        encoding="utf-8",
    )
    response, progress = directory / "response.json", directory / "progress.json"
    request = directory / "request.json"
    request.write_text(
        json.dumps(
            {
                "node": {"prompt": "Synthetic timing only"},
                "response": str(response),
                "progress": str(progress),
            }
        ),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        HERMES_HOME=str(directory),
        PYTHONPATH=os.pathsep.join([str(ROOT / "hermes-workflows-py"), str(fake)]),
    )
    # -S keeps installed host .pth hooks and third-party provider modules out of
    # this synthetic child, even when invoked from a real Hermes environment.
    return (
        [sys.executable, "-S", "-m", "hermes_workflows.agent_worker", str(request)],
        env,
        response,
        progress,
    )


async def measure(name, argv, *, timeout, env=None, response=None, progress=None):
    from hermes_workflows.executor import run_process
    stop = asyncio.Event()
    gaps = []

    async def heartbeat():
        previous = time.perf_counter_ns()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), 0.03)
            except TimeoutError:
                pass
            current = time.perf_counter_ns()
            gaps.append((current - previous) / 1_000_000)
            previous = current

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    start = time.perf_counter_ns()
    row = {"case": name}
    try:
        result = await run_process(argv, env=env, timeout=timeout)
        row.update(result.telemetry, outcome=result.outcome)
        if response is not None:
            marker = None
            if not response.exists():
                row.update(outcome="failure", error_type="MissingWorkerResponse")
            elif result.outcome == "success":
                data = json.loads(response.read_text(encoding="utf-8"))
                row["outcome"] = data["outcome"]
                marker = json.loads(data.get("output") or "{}").get("first_agent_code_ns")
        else:
            match = re.search(r"LATENCY:(\d+)", result.output)
            marker = int(match[1]) if match else None
        if marker is not None:
            row["child_marker_ms"] = round((marker - start) / 1_000_000, 3)
    except Exception as exc:
        row.update(outcome="failure", error_type=type(exc).__name__)
    finally:
        row["wall_ms"] = round((time.perf_counter_ns() - start) / 1_000_000, 3)
        stop.set()
        await beat
        row["loop_max_gap_ms"] = round(max(gaps, default=0), 3)
        if progress is not None and progress.exists():
            row["last_worker_stage"] = json.loads(progress.read_text(encoding="utf-8"))["stage"]
    return row


async def probe(iterations, max_seconds):
    from hermes_workflows.executor import shell_argv
    rows = []
    deadline = time.monotonic() + max_seconds
    cases = [
        (
            "python_first_code",
            [
                sys.executable,
                "-S",
                "-c",
                "import time; print('LATENCY:'+str(time.perf_counter_ns()))",
            ],
        )
    ]
    if os.name == "nt":
        cases += [
            ("cmd", shell_argv("echo LATENCY_OK", "cmd")),
            (
                "powershell",
                shell_argv(
                    "$t=[Diagnostics.Stopwatch]::GetTimestamp(); $f=[Diagnostics.Stopwatch]::Frequency; "
                    "[Console]::WriteLine('LATENCY:'+([long]($t*1000000000.0/$f)))",
                    "powershell",
                ),
            ),
        ]
    with tempfile.TemporaryDirectory(prefix="workflow startup timing żółć ") as temporary:
        directory = Path(temporary)
        worker, env, response, progress = stub_worker(directory)
        for iteration in range(iterations):
            for name, argv in [*cases, ("synthetic_agent_worker", worker)]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return rows, True
                if name == "synthetic_agent_worker":
                    response.unlink(missing_ok=True)
                    progress.unlink(missing_ok=True)
                row = await measure(
                    name,
                    argv,
                    timeout=min(15, remaining),
                    **(
                        {"env": env, "response": response, "progress": progress}
                        if name == "synthetic_agent_worker"
                        else {}
                    ),
                )
                rows.append({"iteration": iteration + 1, **row})
    return rows, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=4, choices=range(1, 21), metavar="1..20")
    parser.add_argument(
        "--max-seconds", type=int, default=60, choices=range(1, 301), metavar="1..300"
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows, exhausted = asyncio.run(probe(args.iterations, args.max_seconds))
    report = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "budget_exhausted": exhausted,
        "service_or_model_calls": 0,
        "production_deadlines_changed": False,
        "heartbeat_interval_ms": 30,
        "results": rows,
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return int(exhausted or any(row["outcome"] != "success" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
