"""The job queue: worker threads that pull queued jobs from SQLite and run
them through the save pipeline, reporting progress back into the job row.

Job types:
  save         one URL (options: force, allow_thin, relocate_domain)
  crawl        link discovery that enqueues child save jobs
  import_file  a SingleFile capture or PDF on disk (options: path, source_url, force)
  update_repos git pull every stored clone and refresh the in-vault READMEs
"""
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from . import crawl
from .db import get_db
from .errors import ContentFreeError
from .jobctx import JobCancelled, Reporter, bind, log
from .pipeline import Saver
from .urls import dedupe_key, is_git_author_url, is_youtube_channel_url

SKIP_OUTCOMES = {"skipped_dedupe", "content_free"}
PROGRESS_WRITE_INTERVAL = 0.5


class JobReporter(Reporter):
    def __init__(self, manager, job_id):
        self.manager = manager
        self.job_id = job_id
        self.db = get_db()
        self.result_text = None
        self._last_write = 0.0
        self._pending = None

    def log(self, line: str):
        for part in line.splitlines() or [""]:
            self.db.append_log(self.job_id, part[:4000])

    def stage(self, text: str):
        self.flush()
        self.db.update_job(self.job_id, stage=text)

    def progress(self, fraction, label=None):
        self._pending = (fraction, label)
        now = time.monotonic()
        if now - self._last_write >= PROGRESS_WRITE_INTERVAL:
            self.flush()

    def flush(self):
        if self._pending is not None:
            fraction, label = self._pending
            self._pending = None
            self._last_write = time.monotonic()
            self.db.update_job(self.job_id, progress=fraction, progress_label=label)

    def result(self, text: str):
        self.result_text = text
        self.db.update_job(self.job_id, result=text)

    def cancelled(self) -> bool:
        return self.manager.cancel_requested(self.job_id)


class JobManager:
    def __init__(self):
        self._cond = threading.Condition()
        self._threads = []
        self._stop = False
        self._paused = False
        self._cancel = set()
        self._running = set()

    # ---- lifecycle -------------------------------------------------------
    def start(self, workers: int):
        db = get_db()
        recovered = db.recover_jobs_after_restart()
        if recovered["requeued"] or recovered["failed"]:
            log(f"Recovered jobs after restart: {recovered}")
        # Pausing survives restarts: a paused queue must not start running
        # (say, a 200-video channel save) just because the container restarted.
        self._paused = db.get_meta("paused") == "1"
        if self._paused:
            log("Queue is paused (resume it in the web UI)")
        self._stop = False
        for i in range(max(1, workers)):
            thread = threading.Thread(target=self._worker, name=f"worker-{i + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def wake(self):
        with self._cond:
            self._cond.notify_all()

    # ---- control ---------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self):
        self._paused = True
        get_db().set_meta("paused", "1")

    def resume(self):
        self._paused = False
        get_db().set_meta("paused", "0")
        self.wake()

    def cancel(self, job_id: int) -> bool:
        db = get_db()
        job = db.get_job(job_id)
        if not job:
            return False
        if job["status"] == "running":
            with self._cond:
                self._cancel.add(job_id)
            db.cancel_job(job_id)  # also cancels its queued children
            return True
        return db.cancel_job(job_id)

    def cancel_requested(self, job_id: int) -> bool:
        with self._cond:
            return job_id in self._cancel

    def running_ids(self) -> set:
        with self._cond:
            return set(self._running)

    # ---- submission -------------------------------------------------------
    def enqueue(self, type_, url=None, options=None, parent_id=None) -> int:
        job_id = get_db().create_job(type_, url, options or {}, parent_id)
        self.wake()
        return job_id

    def submit_url(self, url: str, force=False, allow_thin=False, crawl_options=None) -> dict:
        """Queue one URL. Returns what happened, for the API response."""
        url = (url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"url": url, "status": "invalid", "error": "not an http(s) URL"}
        db = get_db()
        type_ = "crawl" if crawl_options else "save"
        active = db.find_active_job(type_, url)
        if active:
            return {"url": url, "status": "already_queued", "job_id": active["id"]}
        # Channel and author URLs always re-check for new videos/repos.
        bulk = is_youtube_channel_url(url) or is_git_author_url(url)
        if type_ == "save" and not force and not bulk:
            entry = db.get_entry(dedupe_key(url) or "")
            if entry and entry["status"] == "done":
                return {"url": url, "status": "already_saved", "note": entry["note"],
                        "processed_at": entry["processed_at"]}
        options = {"force": bool(force), "allow_thin": bool(allow_thin)}
        if crawl_options:
            options.update({
                "max_depth": int(crawl_options.get("max_depth", 3)),
                "max_pages": int(crawl_options.get("max_pages", 50)),
                "scope": "domain" if crawl_options.get("scope") == "domain" else "path",
                "pdf_only": bool(crawl_options.get("pdf_only", False)),
            })
        job_id = self.enqueue(type_, url, options)
        return {"url": url, "status": "queued", "job_id": job_id}

    def submit_file(self, path: Path, source_url=None, force=False, display_name=None) -> dict:
        name = display_name or path.name
        job_id = self.enqueue("import_file", name,
                              {"path": str(path), "source_url": source_url, "force": bool(force)})
        return {"file": name, "status": "queued", "job_id": job_id}

    # ---- workers -----------------------------------------------------------
    def _worker(self):
        db = get_db()
        while True:
            with self._cond:
                while not self._stop and self._paused:
                    self._cond.wait(timeout=2)
                if self._stop:
                    return
            job = db.claim_next_job()
            if job is None:
                with self._cond:
                    if not self._stop:
                        self._cond.wait(timeout=2)
                continue
            with self._cond:
                self._running.add(job["id"])
            try:
                self._run(job)
            finally:
                with self._cond:
                    self._running.discard(job["id"])
                    self._cancel.discard(job["id"])

    def _run(self, job: dict):
        db = get_db()
        reporter = JobReporter(self, job["id"])
        status, error = "done", None
        with bind(reporter):
            log(f"Starting {job['type']} job: {job['url']}"
                + (f" (attempt {job['attempts']})" if job["attempts"] > 1 else ""))
            try:
                outcome = self._dispatch(job)
                if outcome in SKIP_OUTCOMES:
                    status = "skipped"
            except JobCancelled:
                status, error = "cancelled", "cancelled by user"
                log("Cancelled")
            except ContentFreeError as e:
                status, error = "skipped", str(e)
                log(f"Skipped: {e} (use 'Keep anyway', or tick 'Keep low-content pages', to save it)")
            except Exception as e:  # noqa: BLE001 - a job failure must never kill the worker
                status, error = "failed", f"{type(e).__name__}: {e}"
                log("FAILED: " + error)
                log(traceback.format_exc(limit=8))
            finally:
                reporter.flush()
        db.finish_job(job["id"], status, result=reporter.result_text, error=error)

    def _dispatch(self, job: dict):
        options = job["options"]
        if job["type"] == "save":
            outcome = Saver().save(job["url"], force=options.get("force", False),
                                   allow_thin=options.get("allow_thin", False))
            if options.get("relocate_domain") and outcome not in SKIP_OUTCOMES:
                crawl.relocate_into_domain_folder(job["url"], options["relocate_domain"])
            return outcome
        if job["type"] == "crawl":
            crawl.run_crawl(job["url"], max_depth=options.get("max_depth", 3),
                            max_pages=options.get("max_pages", 50), scope=options.get("scope", "path"),
                            force=options.get("force", False), pdf_only=options.get("pdf_only", False),
                            allow_thin=options.get("allow_thin", False))
            return "done"
        if job["type"] == "update_repos":
            from . import blobs
            blobs.update_repos()
            return "done"
        if job["type"] == "import_file":
            path = Path(options["path"])
            if not path.exists():
                raise FileNotFoundError(f"{path} no longer exists (already imported?)")
            if path.suffix.lower() == ".pdf":
                return Saver().import_pdf_file(path, source_url=options.get("source_url"),
                                               force=options.get("force", False))
            return Saver().import_captured_file(path)
        raise ValueError(f"unknown job type {job['type']!r}")


manager = JobManager()


def enqueue(type_, url=None, options=None, parent_id=None) -> int:
    return manager.enqueue(type_, url, options, parent_id)
