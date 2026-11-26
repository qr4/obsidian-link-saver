"""Per-job logging and progress reporting.

The save pipeline is deep (save -> save_youtube -> download -> transcribe).
Instead of threading a job object through every
call, the worker binds the running job to a context variable and pipeline
code calls the module-level log()/stage()/progress() functions, which reach
whatever job is bound to the current thread (or just stdout when none is,
e.g. from the CLI or tests).
"""
import contextvars
import sys
from contextlib import contextmanager


class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it."""


class Reporter:
    """Interface a running job implements. The default no-op version is
    what log()/stage()/progress() talk to outside a job."""

    job_id = None

    def log(self, line: str):
        pass

    def stage(self, text: str):
        pass

    def progress(self, fraction, label=None):
        pass

    def result(self, text: str):
        pass

    def library_entry(self, query: str):
        pass

    def cancelled(self) -> bool:
        return False


_current = contextvars.ContextVar("saver_job", default=None)
# (start, end) spans that map a sub-step's own 0..1 progress into its slice
# of the parent's bar, e.g. video 3 of 10 -> 0.2..0.3.
_spans = contextvars.ContextVar("saver_progress_spans", default=())


@contextmanager
def bind(reporter: Reporter):
    token = _current.set(reporter)
    span_token = _spans.set(())
    try:
        yield reporter
    finally:
        _spans.reset(span_token)
        _current.reset(token)


def current():
    return _current.get()


def log(*args):
    message = " ".join(str(a) for a in args)
    reporter = _current.get()
    prefix = f"[job {reporter.job_id}] " if reporter and reporter.job_id else ""
    for line in message.splitlines() or [""]:
        print(prefix + line, file=sys.stdout, flush=True)
    if reporter:
        reporter.log(message)


def stage(text: str):
    """A named step of the job ("Capturing page", "Transcribing"). Resets
    the bar to indeterminate until the step reports progress of its own."""
    log(f"» {text}")
    reporter = _current.get()
    if reporter:
        reporter.stage(text)
        reporter.progress(_map(None), None)


def _map(fraction):
    if fraction is None:
        return None
    for start, end in reversed(_spans.get()):
        fraction = start + max(0.0, min(1.0, fraction)) * (end - start)
    return fraction


def progress(fraction, label=None):
    """fraction in 0..1, or None for indeterminate."""
    reporter = _current.get()
    if reporter:
        reporter.progress(_map(fraction), label)


@contextmanager
def span(start: float, end: float):
    token = _spans.set(_spans.get() + ((start, end),))
    try:
        yield
    finally:
        _spans.reset(token)


def result(text: str):
    """The job's one-line outcome ("Saved -> Areas/..."), shown in the UI."""
    log(text)
    reporter = _current.get()
    if reporter:
        reporter.result(text)


def library_entry(query: str):
    """What finds this job's entry in the library search (a URL, or a
    filename). Lets the UI link a file import - labelled with the uploaded
    file's name - to the entry it ended up as."""
    reporter = _current.get()
    if reporter:
        reporter.library_entry(query)


def check_cancelled():
    reporter = _current.get()
    if reporter and reporter.cancelled():
        raise JobCancelled("cancelled by user")
