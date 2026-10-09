"""Real local CPU subprocess checks; no database, network, or model provider."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from ragos.runtime.process_runner import ProcessRunner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group contract")


def run(code, **kwargs):
    return ProcessRunner(termination_grace_seconds=0.1).run(
        (sys.executable, "-c", code),
        deadline_seconds=kwargs.pop("deadline_seconds", 2),
        cancellation=kwargs.pop("cancellation", lambda: False),
        heartbeat=kwargs.pop("heartbeat", lambda: None),
        env=kwargs.pop("env", {}),
        heartbeat_interval=0.02,
        **kwargs,
    )


def test_real_process_success_nonzero_and_no_ambient_environment(monkeypatch):
    monkeypatch.setenv("RAGOS_AMBIENT_SECRET", "must-not-be-inherited")
    assert run("import os; assert 'RAGOS_AMBIENT_SECRET' not in os.environ").error_code is None
    result = run("raise SystemExit(7)")
    assert (result.exit_code, result.error_code) == (7, "TASK_PROCESS_FAILED")
    assert (
        run("import os; assert os.environ['EXPLICIT'] == 'yes'", env={"EXPLICIT": "yes"}).exit_code
        == 0
    )


def test_real_process_deadline():
    start = time.monotonic()
    result = run("import time; time.sleep(10)", deadline_seconds=0.1)
    assert result.error_code == "TASK_TIMEOUT"
    assert result.exit_code < 0
    assert time.monotonic() - start < 2


def test_real_process_cancel_and_fence_loss():
    started = time.monotonic()
    result = run(
        "import time; time.sleep(10)", cancellation=lambda: time.monotonic() - started > 0.1
    )
    assert result.error_code == "TASK_CANCELLED"
    calls = []

    def heartbeat():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("lease superseded")

    result = run("import time; time.sleep(10)", heartbeat=heartbeat)
    assert result.error_code == "TASK_FENCE_LOST"
    assert result.exit_code < 0


def running(pid: int) -> bool:
    try:
        # A dead orphan zombie is not executing. Its adopting init owns reaping.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("parent_exits", [False, True])
def test_real_descendant_group_is_killed_even_if_parent_exits(tmp_path, parent_exits):
    pid_file = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    code = (
        "import subprocess,sys,time,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        + ("time.sleep(0.08)" if parent_exits else "time.sleep(30)")
    )
    result = run(code, deadline_seconds=0.2)
    assert result.error_code == (None if parent_exits else "TASK_TIMEOUT")
    pid = int(pid_file.read_text())
    end = time.monotonic() + 1
    while running(pid) and time.monotonic() < end:
        time.sleep(0.01)
    assert not running(pid), f"descendant {pid} survived group cleanup"


def test_cancel_before_start_does_not_create_process(tmp_path):
    marker = tmp_path / "unexpected"
    result = run(
        f"from pathlib import Path; Path({str(marker)!r}).touch()", cancellation=lambda: True
    )
    assert (result.exit_code, result.error_code) == (None, "TASK_CANCELLED")
    assert not marker.exists()


def test_real_process_large_output_does_not_block():
    assert (
        run("import sys; sys.stdout.write('x'*1000000); sys.stderr.write('y'*1000000)").exit_code
        == 0
    )


def test_deadline_watchdog_stops_work_while_database_callback_is_delayed(tmp_path):
    marker = tmp_path / "late-work"
    calls = 0

    def cancellation():
        nonlocal calls
        calls += 1
        if calls > 1:
            time.sleep(0.3)
        return False

    command = (
        sys.executable,
        "-c",
        "import pathlib,time; time.sleep(0.2); "
        f"pathlib.Path({str(marker)!r}).write_text('should not happen')",
    )
    result = ProcessRunner(termination_grace_seconds=0.01).run(
        command, deadline_seconds=0.08, cancellation=cancellation, heartbeat=lambda: None, env={}
    )
    assert result.error_code == "TASK_TIMEOUT"
    assert not marker.exists()


def test_worker_shutdown_stops_process_during_a_delayed_db_callback(tmp_path):
    import threading

    stop = threading.Event()
    calls = 0
    marker = tmp_path / "shutdown-late-work"

    def cancellation():
        nonlocal calls
        calls += 1
        if calls > 1:
            stop.set()
            time.sleep(0.3)
        return False

    command = (
        sys.executable,
        "-c",
        f"import pathlib,time; time.sleep(0.2); pathlib.Path({str(marker)!r}).write_text('late')",
    )
    result = ProcessRunner(termination_grace_seconds=0.01).run(
        command,
        deadline_seconds=2,
        cancellation=cancellation,
        heartbeat=lambda: None,
        env={},
        shutdown=stop.is_set,
    )
    assert result.error_code == "TASK_WORKER_STOPPED"
    assert not marker.exists()
