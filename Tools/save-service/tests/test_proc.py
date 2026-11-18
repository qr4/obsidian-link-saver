import subprocess
import time

import pytest

from saver import proc
from saver.jobctx import JobCancelled, Reporter, bind


class Flag(Reporter):
    def __init__(self):
        self.stop = False

    def cancelled(self):
        return self.stop


def test_run_returns_output():
    result = proc.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_run_times_out():
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(["sleep", "5"], timeout=0.6)


def test_run_is_cancellable():
    flag = Flag()
    start = time.monotonic()
    with bind(flag):
        import threading
        threading.Timer(0.3, lambda: setattr(flag, "stop", True)).start()
        with pytest.raises(JobCancelled):
            proc.run(["sleep", "30"])
    assert time.monotonic() - start < 3
