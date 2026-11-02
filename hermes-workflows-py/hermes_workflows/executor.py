"""Native executors: no Bun, POSIX signals on Windows, or shell-built paths."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .process_launch import ProcessLaunch


@dataclass
class Result:
    outcome: str = "success"
    output: str = ""
    error: str | None = None
    telemetry: dict = field(default_factory=dict)


class WindowsJob:
    """KILL_ON_JOB_CLOSE also cleans grandchildren if the plugin process exits."""

    def __init__(self, process: subprocess.Popen):
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def shell_argv(command: str, shell: str = "shell") -> list[str]:
    if shell in ("powershell", "pwsh"):
        executable = shutil.which(shell) or (
            "powershell.exe" if shell == "powershell" else "pwsh.exe"
        )
        # Propagate cmdlet errors and native executable exit codes to the DAG.
        script = (
            "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';\n"
            + "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding;\n"
            + command
            + "\nif ($LASTEXITCODE) { exit $LASTEXITCODE }"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ]
    if shell == "cmd" or shell == "shell" and os.name == "nt":
        return [
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/s",
            "/c",
            command,
        ]
    if shell in ("shell", "sh"):
        return [shutil.which("sh") or "/bin/sh", "-c", command]
    raise ValueError(f"unknown shell: {shell}")


async def run_process(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float = 1800,
    max_output: int = 2_000_000,
) -> Result:
    """File capture avoids pipe deadlocks even with chatty grandchildren."""
    started = time.monotonic()
    launch_state = ProcessLaunch(WindowsJob)
    try:
        flags = (
            subprocess.CREATE_NO_WINDOW | 0x00000004 if os.name == "nt" else 0
        )  # CREATE_SUSPENDED
        # cmd.exe parses its /c tail itself; CRT list2cmdline escapes embedded
        # quotes as \" which cmd treats literally (breaking redirections and
        # paths). /s strips our enclosing pair and retains the command verbatim.
        launch = (
            subprocess.list2cmdline(argv[:-1]) + ' "' + argv[-1] + '"'
            if os.name == "nt"
            and len(argv) >= 3
            and argv[-2].lower() == "/c"
            and Path(argv[0]).name.lower() in ("cmd", "cmd.exe")
            else argv
        )
        launch_task = asyncio.create_task(
            asyncio.to_thread(
                launch_state.start,
                launch,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
        )

        def observe(task):
            if not task.cancelled():
                error = task.exception()
                if error is not None and launch_state.abandoned.is_set():
                    logging.getLogger(__name__).warning(
                        "Late process launch failed (%s)", type(error).__name__
                    )

        launch_task.add_done_callback(observe)
        # Python 3.11 wait_for can swallow external cancellation when its
        # future completes concurrently. Keep the launch shielded, but apply
        # the deadline to this task so cancellation always reaches cleanup.
        async with asyncio.timeout(max(0, timeout - (time.monotonic() - started))):
            await asyncio.shield(launch_task)
        launch_ms = round((time.monotonic() - started) * 1000)
        process = launch_state.process
        while process.poll() is None:
            if time.monotonic() - started >= timeout:
                raise TimeoutError(f"step exceeded {timeout:g} seconds")
            await asyncio.sleep(0.03)
        stdout, stderr = await asyncio.to_thread(launch_state.output, max_output)

        def decode(data):
            try:
                text = data[:max_output].decode("utf-8-sig")
            except UnicodeDecodeError:
                encoding = (
                    "cp" + str(ctypes.windll.kernel32.GetOEMCP()) if os.name == "nt" else "utf-8"
                )
                text = data[:max_output].decode(encoding, errors="replace")
            return text + ("\n[output truncated]" if len(data) > max_output else "")

        output, error = decode(stdout), decode(stderr)
        return Result(
            "success" if process.returncode == 0 else "failure",
            output + ("\n[stderr]\n" + error if error else ""),
            (error or f"process exited with code {process.returncode}")
            if process.returncode
            else None,
            {"duration_ms": round((time.monotonic() - started) * 1000), "launch_ms": launch_ms},
        )
    except TimeoutError:
        raise TimeoutError(f"step exceeded {timeout:g} seconds") from None
    finally:
        cleanup = launch_state.abandon()
        while cleanup is not None and cleanup.is_alive():
            await asyncio.sleep(0.01)


class Executor:
    def __init__(
        self,
        agent_runner: Callable[[dict, dict], Awaitable[Result]] | None = None,
        wait_runner: Callable[[dict, dict], Awaitable[Result | None]] | None = None,
        home: Path | None = None,
    ):
        self.agent_runner = agent_runner
        self.wait_runner = wait_runner
        self.home = home

    async def execute(self, node: dict, run: dict, settings: dict) -> Result:
        timeout = node.get("timeout_seconds", settings["default_timeout_seconds"])
        cwd = str(Path(node["workdir"]).expanduser().resolve()) if node.get("workdir") else None
        if node["type"] == "script":
            if not settings["scripts_enabled"]:
                raise ValueError("script execution is disabled in Settings")
            allowed = {
                v.strip().upper() for v in settings["script_env_allowlist"].split(",") if v.strip()
            }
            requested = {v.upper() for v in node.get("env", allowed)}
            env = {k: v for k, v in os.environ.items() if k.upper() in allowed & requested}
            # Windows requires these to locate core DLLs, shells and temp files.
            if os.name == "nt":
                env.update(
                    {
                        k: v
                        for k, v in os.environ.items()
                        if k.upper()
                        in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT"}
                    }
                )
                env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
            env["PYTHONIOENCODING"] = "utf-8"
            return await run_process(
                shell_argv(node["command"], node.get("shell", settings["default_shell"])),
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        if node["type"] == "agent_task":
            if self.agent_runner:
                async with asyncio.timeout(timeout):
                    return await self.agent_runner(node, run)
            with tempfile.TemporaryDirectory(prefix="hermes-workflow-") as temporary:
                root = Path(temporary)
                request, response = root / "request.json", root / "response.json"
                progress = root / "progress.json"
                request.write_text(
                    json.dumps(
                        {
                            "node": node,
                            "run_id": run["run_id"],
                            "response": str(response),
                            "progress": str(progress),
                            "preflight": node.get("_preflight", False),
                            "telemetry": node.get("_telemetry_context"),
                            "worker_approvals_enabled": settings.get(
                                "worker_approvals_enabled", False
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                env = dict(
                    os.environ,
                    PYTHONIOENCODING="utf-8",
                    HERMES_WORKFLOWS_WORKER="1",
                    HERMES_DISABLE_LAZY_INSTALLS="1",
                )
                # Capture the parent profile explicitly; the worker is a fresh
                # process and cannot inherit Hermes' ContextVar home override.
                from .db import hermes_home

                env["HERMES_HOME"] = str(self.home or hermes_home())
                # Preserve the host checkout/module search paths in the isolated
                # Python worker without modifying the gateway's profile or cwd.
                env["PYTHONPATH"] = os.pathsep.join(
                    dict.fromkeys(
                        [
                            str(Path(__file__).resolve().parent.parent),
                            *[str(Path(p).resolve()) for p in sys.path if p],
                            env.get("PYTHONPATH", ""),
                        ]
                    )
                )
                try:
                    result = await run_process(
                        [sys.executable, "-m", "hermes_workflows.agent_worker", str(request)],
                        cwd=cwd,
                        env=env,
                        timeout=timeout,
                    )
                except TimeoutError:
                    stage = "starting_worker"
                    try:
                        stage = json.loads(progress.read_text(encoding="utf-8"))["stage"]
                    except (OSError, ValueError, KeyError):
                        pass
                    return Result(
                        "failure", error=f"Hermes worker exceeded {timeout:g}s during {stage}"
                    )
                if response.exists():
                    data = json.loads(response.read_text(encoding="utf-8"))
                    return Result(**data)
                return Result(
                    "failure", result.output, result.error or "Hermes worker produced no response"
                )
        if node["type"] == "wait":
            since = run["nodes"][node["id"]].get("wait_started_at", time.time())
            while True:
                if time.time() - since >= timeout:
                    raise TimeoutError("external wait timed out")
                result = (
                    await self.wait_runner(node, run)
                    if self.wait_runner
                    else await self._github_wait(node)
                )
                if result is not None:
                    return result
                await asyncio.sleep(min(10, max(0.05, timeout - (time.time() - since))))
        raise ValueError(f"no executor for {node['type']}")

    @staticmethod
    async def _github_wait(node: dict) -> Result | None:
        import re
        import urllib.error
        import urllib.request

        ref = node["wait_for"]["github_pr_merged"].strip()
        if not ref or ref.startswith("-") or any(c in ref for c in "\x00\r\n"):
            raise ValueError("github_pr_merged requires a PR URL, number or branch reference")
        gh = shutil.which("gh")
        if gh:
            try:
                response = await run_process(
                    [gh, "pr", "view", ref, "--json", "state"],
                    cwd=node.get("workdir"), env=dict(os.environ), timeout=30,
                )
            except OSError:
                return None
            if response.outcome != "success":
                return None
            try:
                state = json.loads(response.output).get("state")
            except (ValueError, AttributeError):
                return None
            if state == "MERGED":
                return Result(output=ref)
            if state == "CLOSED":
                return Result("failure", ref, "pull request closed without merging")
            return None
        # Keep the original stdlib-only URL path usable without the optional
        # GitHub CLI. Contextual references require gh's repository discovery.
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?", ref
        )
        if not match:
            return None
        owner, repo, number = match.groups()

        def fetch():
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-workflows-py"}
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if token:
                headers["Authorization"] = "Bearer " + token
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}", headers=headers
                ),
                timeout=15,
            ) as response:
                return json.load(response)

        try:
            data = await asyncio.to_thread(fetch)
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("merged"):
            return Result(output=ref)
        if data.get("state") == "closed":
            return Result("failure", ref, "pull request closed without merging")
        return None
