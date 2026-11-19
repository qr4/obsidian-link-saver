"""subprocess.run() replacement that honours job cancellation.

A plain subprocess.run() blocks until the child exits - a yt-dlp channel
listing, a git clone or a SingleFile capture could keep running for
minutes after the user hit Cancel. This polls every half second, and kills
the child (raising JobCancelled) as soon as the job is cancelled.
"""
import subprocess
import time

from .jobctx import JobCancelled, current

POLL_SECONDS = 0.5


def run(cmd, timeout=None, text=True, **kwargs) -> subprocess.CompletedProcess:
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, **kwargs)
    deadline = time.monotonic() + timeout if timeout else None
    reporter = current()
    while True:
        try:
            # communicate() may be retried after a timeout without losing output
            stdout, stderr = process.communicate(timeout=POLL_SECONDS)
            return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            cancelled = reporter is not None and reporter.cancelled()
            if cancelled or (deadline is not None and time.monotonic() > deadline):
                process.kill()
                process.communicate()
                if cancelled:
                    raise JobCancelled("cancelled by user") from None
                raise subprocess.TimeoutExpired(cmd, timeout) from None
