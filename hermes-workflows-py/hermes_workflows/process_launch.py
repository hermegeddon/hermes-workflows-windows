"""Off-loop process setup whose owner survives a cancelled or timed-out await."""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import tempfile
import threading


class ProcessLaunch:
    def __init__(self, job_factory):
        self.job_factory = job_factory
        self.process = self.job = self.out = self.err = None
        self.finished = threading.Event()
        self.abandoned = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.stopped = False

    def start(self, argv, **kwargs):
        # This entire method runs in the executor thread. In particular, the
        # capture files must outlive a late Popen even if its awaiter has left.
        try:
            if self.abandoned.is_set():
                return
            self.out, self.err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            self.process = subprocess.Popen(argv, stdout=self.out, stderr=self.err, **kwargs)
            if os.name == "nt":
                self.job = self.job_factory(self.process)
                if not self.abandoned.is_set():
                    resume = ctypes.WinDLL("ntdll").NtResumeProcess
                    resume.argtypes = [ctypes.c_void_p]
                    resume.restype = ctypes.c_long
                    if resume(int(self.process._handle)) < 0:
                        raise OSError("could not resume Windows worker")
        finally:
            # Either the awaiter's finally sees finished, or this thread sees
            # abandoned. Both orders release every resource; close is idempotent.
            self.finished.set()
            if self.abandoned.is_set():
                self.close()

    def _stop(self):
        if self.process is None or self.stopped:
            return
        if self.job is not None:
            self.job.close()
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            # Includes a suspended Windows child if job assignment failed.
            self.process.kill()
            self.process.wait(5)
        self.stopped = True

    def output(self, limit):
        with self.lock:
            try:
                # Reap grandchildren before reading capture, just as on cancellation.
                self._stop()
                self.out.seek(0)
                self.err.seek(0)
                return self.out.read(limit + 1), self.err.read(limit + 1)
            finally:
                self._close_files()

    def _close_files(self):
        for stream in (self.out, self.err):
            if stream is not None:
                stream.close()
        self.closed.set()

    def abandon(self):
        self.abandoned.set()
        if not self.finished.is_set() or self.closed.is_set():
            # A late startup thread owns cleanup itself.
            return None

        def cleanup():
            try:
                self.close()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Process cleanup failed (%s)", type(exc).__name__
                )

        # Cleanup must not queue behind other blocked launches in the default
        # executor, or be lost if an asyncio Future is cancelled a second time.
        thread = threading.Thread(target=cleanup, name="workflow-process-cleanup", daemon=True)
        thread.start()
        return thread

    def close(self):
        with self.lock:
            if self.closed.is_set():
                return
            try:
                self._stop()
            finally:
                self._close_files()
