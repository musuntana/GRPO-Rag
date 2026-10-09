"""Bounded Linux process groups for server-registered task commands."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ragos.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class TaskProcessResult:
    exit_code: int | None
    error_code: str | None


class ProcessRunner:
    """Run a trusted registry argv in its own Linux process group.

    This is lifecycle isolation, not a security sandbox. The caller must restrict
    commands and build an explicit environment. A command that creates a new
    session can escape the group and requires a cgroup/sandbox execution adapter.
    Output goes to DEVNULL, so unbounded task output cannot block or fill memory.
    """

    def __init__(
        self, *, termination_grace_seconds: float = 0.5, poll_seconds: float = 0.02
    ) -> None:
        self._require_duration(termination_grace_seconds, "termination grace")
        self._require_duration(poll_seconds, "poll interval")
        self.termination_grace_seconds = termination_grace_seconds
        self.poll_seconds = poll_seconds

    @staticmethod
    def _require_duration(value: float, name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValidationError(f"{name} must be a positive finite duration")

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def _stop_group(self, process: subprocess.Popen) -> None:
        # Run even when the group leader exited: it may have left descendants.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        end = time.monotonic() + self.termination_grace_seconds
        while self._group_exists(process.pid) and time.monotonic() < end:
            process.poll()  # reap the direct child as soon as it exits
            time.sleep(min(self.poll_seconds, max(0.0, end - time.monotonic())))
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()  # SIGKILL cannot be ignored; release the direct child.

    def run(
        self,
        argv: tuple[str, ...],
        *,
        deadline_seconds: float,
        cancellation: Callable[[], bool],
        heartbeat: Callable[[], None],
        env: Mapping[str, str],
        heartbeat_interval: float = 1.0,
        shutdown: Callable[[], bool] | None = None,
    ) -> TaskProcessResult:
        if sys.platform != "linux":
            raise ValidationError("process group task runner requires Linux")
        self._require_duration(deadline_seconds, "deadline")
        self._require_duration(heartbeat_interval, "heartbeat interval")
        if (
            not isinstance(argv, tuple)
            or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not os.path.isabs(argv[0])
        ):
            raise ValidationError("task argv requires a server-registered absolute executable")
        if not callable(cancellation) or not callable(heartbeat):
            raise ValidationError("task runner requires cancellation and heartbeat callbacks")
        environment = dict(env)
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in environment.items()
        ):
            raise ValidationError("task environment must contain explicit valid string entries")
        deadline = time.monotonic() + deadline_seconds
        process = None
        error_code = None
        watchdog_stop = threading.Event()
        watchdog_fired = threading.Event()
        shutdown_fired = threading.Event()
        watchdog = None
        try:
            # Check the fence and cancellation before starting side effects.
            try:
                if cancellation():
                    return TaskProcessResult(None, "TASK_CANCELLED")
                heartbeat()
            except Exception:
                return TaskProcessResult(None, "TASK_FENCE_LOST")
            if time.monotonic() >= deadline:
                return TaskProcessResult(None, "TASK_TIMEOUT")
            try:
                process = subprocess.Popen(
                    argv,
                    shell=False,
                    start_new_session=True,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError:
                return TaskProcessResult(None, "TASK_PROCESS_START_FAILED")

            def enforce_deadline():
                while not watchdog_stop.wait(
                    min(self.poll_seconds, max(0.0, deadline - time.monotonic()))
                ):
                    if shutdown is not None and shutdown():
                        shutdown_fired.set()
                        break
                    if time.monotonic() >= deadline:
                        watchdog_fired.set()
                        break
                else:
                    return
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            # The deadline remains effective even if a DB callback blocks temporarily.
            watchdog = threading.Thread(target=enforce_deadline, daemon=True)
            watchdog.start()
            next_heartbeat = time.monotonic() + heartbeat_interval
            while True:
                now = time.monotonic()
                # Never accept success after cancellation, expiry, or lost fence.
                if shutdown_fired.is_set() or (shutdown is not None and shutdown()):
                    error_code = "TASK_WORKER_STOPPED"
                    break
                if now >= deadline:
                    error_code = "TASK_TIMEOUT"
                    break
                try:
                    if cancellation():
                        error_code = "TASK_CANCELLED"
                        break
                    if now >= next_heartbeat:
                        heartbeat()
                        next_heartbeat = time.monotonic() + heartbeat_interval
                except Exception:
                    error_code = "TASK_FENCE_LOST"
                    break
                if shutdown_fired.is_set():
                    error_code = "TASK_WORKER_STOPPED"
                    break
                if watchdog_fired.is_set() or time.monotonic() >= deadline:
                    error_code = "TASK_TIMEOUT"
                    break
                exit_code = process.poll()
                if exit_code is not None:
                    error_code = None if exit_code == 0 else "TASK_PROCESS_FAILED"
                    break
                time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))
        finally:
            watchdog_stop.set()
            if watchdog is not None:
                watchdog.join()
            if process is not None:
                self._stop_group(process)
        return TaskProcessResult(process.returncode, error_code)
