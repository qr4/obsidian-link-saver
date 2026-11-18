"""SQLite storage: the dedupe registry, the
job queue and per-job logs.

One connection shared by all threads behind a lock; WAL mode so the web UI
can read while a worker writes. Each public method is one short
transaction - nothing here is held open across the slow
download/LLM/transcription work.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per saved source, keyed "url:<normalized>".
CREATE TABLE IF NOT EXISTS entries (
    key          TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    note         TEXT,            -- "Areas/..." or "Dumps/git/<host>/<owner>/<repo>" etc.
    kind         TEXT,            -- page | git | pdf | youtube | video | other
    filename     TEXT,            -- single_page / pdf dump file, so --force overwrites it
    area_note    TEXT,            -- leaf note, relative to Areas/
    area_entry   TEXT,            -- exact bullet text, so --force can find and replace it
    status       TEXT NOT NULL DEFAULT 'done',   -- 'in_progress' while a job holds the claim
    processed_at TEXT NOT NULL,   -- last (re)save
    created_at   TEXT NOT NULL,   -- first save
    job_id       INTEGER
);
CREATE INDEX IF NOT EXISTS entries_created_at ON entries(created_at);
CREATE INDEX IF NOT EXISTS entries_kind ON entries(kind);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,   -- save | crawl | import_file
    url            TEXT,
    options        TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,   -- queued | running | done | skipped | failed | cancelled
    stage          TEXT,
    progress       REAL,
    progress_label TEXT,
    result         TEXT,
    error          TEXT,
    parent_id      INTEGER,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id);

CREATE TABLE IF NOT EXISTS job_logs (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    ts     TEXT NOT NULL,
    line   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_logs_job ON job_logs(job_id, id);
"""

FINISHED_STATUSES = ("done", "skipped", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")
ENTRY_FIELDS = ("url", "note", "filename", "area_note", "area_entry")
JOB_FIELDS = ("status", "stage", "progress", "progress_label", "result", "error",
              "started_at", "finished_at", "options", "attempts")
MAX_LOG_LINES_PER_JOB = 3000
MAX_JOB_ATTEMPTS = 3


def now_iso() -> str:
    # Local time, seconds precision - the format registry exports use, so
    # imported and new rows sort and group together.
    return datetime.now().isoformat(timespec="seconds")


def kind_for_note(note) -> str:
    note = note or ""
    for prefix, kind in (("Dumps/git", "git"), ("Dumps/pdf", "pdf"), ("Dumps/youtube", "youtube"),
                         ("Dumps/video", "video"), ("Areas", "page")):
        if note.startswith(prefix):
            return kind
    return "other"


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
        self.canonicalize_keys()

    def canonicalize_keys(self) -> int:
        """Rewrite git-host entry keys to the current normalize_url form
        (owner/repo lowercased, no `.git`), so lookups stay exact. Keys made
        before that rule, or imported from an export, could hold the same
        repo twice (url:github.com/SigNoz/signoz and url:github.com/signoz/signoz).
        Such a duplicate is dropped in favour of the row already at the
        canonical key. Only git hosts are touched; returns rows changed."""
        from .urls import GIST_HOST, GIT_HOSTS, normalize_url

        changed = 0
        with self.tx() as c:
            for row in c.execute("SELECT key FROM entries WHERE key LIKE 'url:%'").fetchall():
                key = row["key"]
                host = key[4:].split("/", 1)[0]
                if host not in GIT_HOSTS and host != GIST_HOST:
                    continue
                canonical = f"url:{normalize_url('https://' + key[4:])}"
                if canonical == key:
                    continue
                if c.execute("SELECT 1 FROM entries WHERE key = ?", (canonical,)).fetchone():
                    c.execute("DELETE FROM entries WHERE key = ?", (key,))
                else:
                    c.execute("UPDATE entries SET key = ? WHERE key = ?", (canonical, key))
                changed += 1
        return changed

    def close(self):
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _rows(self, sql, params=()) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # meta (small persistent flags, e.g. paused)
    # ------------------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self._row("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.tx() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ------------------------------------------------------------------
    # entries (dedupe registry)
    # ------------------------------------------------------------------
    def get_entry(self, key):
        return self._row("SELECT * FROM entries WHERE key = ?", (key,))

    def entry_by_note(self, note):
        """The entry whose stored note is exactly this path, e.g. a single
        video's folder (Dumps/video/media.ccc.de/<conference>/<talk>)."""
        return self._row("SELECT * FROM entries WHERE note = ? ORDER BY processed_at DESC LIMIT 1", (note,))

    def has_entry(self, key) -> bool:
        return self.get_entry(key) is not None

    def claim_entry(self, key, url, job_id=None, force=False):
        """Atomically check-and-claim a dedupe key before the slow work, so
        two jobs racing on the same URL (a crawl reaching one hub page via
        two link paths) can't both see "not saved yet" and both proceed.

        Returns (state, row): ("exists"|"in_progress", existing row) when
        the URL must be skipped, or ("claimed", prior row or None). With
        force the prior row is returned so the caller can overwrite the
        same dump file and replace the same Area bullet.

        A re-save (force) keeps the previous record's fields while it
        runs: if the redo fails, release_claim()
        restores it instead of the URL losing its record altogether."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM entries WHERE key = ?", (key,)).fetchone()
            row = dict(row) if row else None
            if row and not force:
                return ("in_progress" if row["status"] == "in_progress" else "exists"), row
            now = now_iso()
            if row:
                c.execute("UPDATE entries SET status = 'in_progress', job_id = ? WHERE key = ?", (job_id, key))
            else:
                c.execute(
                    "INSERT INTO entries (key, url, status, processed_at, created_at, job_id) "
                    "VALUES (?, ?, 'in_progress', ?, ?, ?)",
                    (key, url, now, now, job_id))
            return "claimed", row

    def release_claim(self, key):
        """Undo claim_entry after a failed save: restore a previously
        completed record, or drop a fresh placeholder so the URL doesn't
        masquerade as saved with nothing behind it."""
        with self.tx() as c:
            row = c.execute("SELECT note, status FROM entries WHERE key = ?", (key,)).fetchone()
            if not row or row["status"] != "in_progress":
                return
            if row["note"]:
                c.execute("UPDATE entries SET status = 'done' WHERE key = ?", (key,))
            else:
                c.execute("DELETE FROM entries WHERE key = ?", (key,))

    def complete_entry(self, key, *, url, note, filename=None, area_note=None, area_entry=None, job_id=None):
        """Store the finished save. Fields not given are cleared: the record
        is replaced as a whole."""
        now = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO entries (key, url, note, kind, filename, area_note, area_entry,
                                        status, processed_at, created_at, job_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       url = excluded.url, note = excluded.note, kind = excluded.kind,
                       filename = excluded.filename, area_note = excluded.area_note,
                       area_entry = excluded.area_entry, status = 'done',
                       processed_at = excluded.processed_at, job_id = excluded.job_id""",
                (key, url, note, kind_for_note(note), filename, area_note, area_entry, now, now, job_id))

    def update_entry_fields(self, key, **values):
        values = {k: v for k, v in values.items() if k in ENTRY_FIELDS}
        if not values:
            return
        if "note" in values:
            values["kind"] = kind_for_note(values["note"])
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.tx() as c:
            c.execute(f"UPDATE entries SET {assignments} WHERE key = ?", (*values.values(), key))

    def delete_entry(self, key) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM entries WHERE key = ?", (key,)).rowcount > 0

    def clear_stale_claims(self) -> int:
        """At startup nothing can be mid-save, so every in_progress claim
        is left over from a crash."""
        with self.tx() as c:
            restored = c.execute(
                "UPDATE entries SET status = 'done' WHERE status = 'in_progress' AND note IS NOT NULL").rowcount
            dropped = c.execute("DELETE FROM entries WHERE status = 'in_progress'").rowcount
        return restored + dropped

    def list_entries(self, q=None, kind=None, limit=50, offset=0):
        where, params = ["status = 'done'"], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if q:
            like = f"%{q}%"
            where.append("(url LIKE ? OR note LIKE ? OR area_note LIKE ? OR filename LIKE ? OR area_entry LIKE ?)")
            params += [like] * 5
        clause = " AND ".join(where)
        total = self._row(f"SELECT COUNT(*) AS n FROM entries WHERE {clause}", params)["n"]
        rows = self._rows(
            f"SELECT * FROM entries WHERE {clause} ORDER BY processed_at DESC, key LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return rows, total

    def export_registry(self) -> dict:
        """The registry as one JSON object, keyed by entry key (the shape
        import_registry() reads back)."""
        registry = {}
        for row in self._rows("SELECT * FROM entries WHERE status = 'done' ORDER BY created_at, key"):
            record = {"url": row["url"], "note": row["note"], "processed_at": row["processed_at"]}
            for name in ("filename", "area_note", "area_entry"):
                if row[name] is not None:
                    record[name] = row[name]
            registry[row["key"]] = record
        return registry

    def import_registry(self, registry: dict, overwrite: bool = False) -> dict:
        imported = skipped = 0
        with self.tx() as c:
            for key, record in registry.items():
                if not isinstance(record, dict) or record.get("in_progress") or not record.get("url"):
                    skipped += 1
                    continue
                exists = c.execute("SELECT 1 FROM entries WHERE key = ?", (key,)).fetchone()
                if exists and not overwrite:
                    skipped += 1
                    continue
                stamp = record.get("processed_at") or now_iso()
                c.execute(
                    """INSERT OR REPLACE INTO entries (key, url, note, kind, filename, area_note,
                                                       area_entry, status, processed_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)""",
                    (key, record["url"], record.get("note"), kind_for_note(record.get("note")),
                     record.get("filename"), record.get("area_note"), record.get("area_entry"), stamp, stamp))
                imported += 1
        self.canonicalize_keys()  # an export may predate the git-host rule
        return {"imported": imported, "skipped": skipped}

    def patch_entries(self, patches: list) -> dict:
        """Bulk field edits from the vault-maintenance skill after it moved
        notes around: [{"key": ..., "set": {...}, "unset": [...]}, ...]."""
        updated, missing, rejected = 0, [], []
        with self.tx() as c:
            for patch in patches:
                key = patch.get("key")
                values = dict(patch.get("set") or {})
                for name in patch.get("unset") or []:
                    values[name] = None
                bad = [k for k in values if k not in ENTRY_FIELDS]
                if bad or not key or ("url" in values and not values["url"]):
                    rejected.append(key)
                    continue
                if not c.execute("SELECT 1 FROM entries WHERE key = ?", (key,)).fetchone():
                    missing.append(key)
                    continue
                if not values:
                    continue
                if "note" in values:
                    values["kind"] = kind_for_note(values["note"])
                assignments = ", ".join(f"{k} = ?" for k in values)
                c.execute(f"UPDATE entries SET {assignments} WHERE key = ?", (*values.values(), key))
                updated += 1
        return {"updated": updated, "missing": missing, "rejected": rejected}

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    def create_job(self, type_, url=None, options=None, parent_id=None) -> int:
        now = now_iso()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO jobs (type, url, options, status, parent_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (type_, url, json.dumps(options or {}), parent_id, now, now))
            return cur.lastrowid

    def claim_next_job(self):
        with self.tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            now = now_iso()
            c.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?, attempts = attempts + 1, "
                "stage = NULL, progress = NULL, progress_label = NULL, error = NULL WHERE id = ?",
                (now, now, row["id"]))
            job = dict(row)
            job.update(status="running", started_at=now, attempts=row["attempts"] + 1)
            return self._decode_job(job)

    def update_job(self, job_id, **values):
        values = {k: v for k, v in values.items() if k in JOB_FIELDS}
        if "options" in values and not isinstance(values["options"], str):
            values["options"] = json.dumps(values["options"])
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values.values(), job_id))

    def finish_job(self, job_id, status, result=None, error=None):
        now = now_iso()
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET status = ?, result = COALESCE(?, result), error = ?, finished_at = ?, "
                "updated_at = ?, progress = CASE WHEN ? = 'done' THEN 1.0 ELSE progress END WHERE id = ?",
                (status, result, error, now, now, status, job_id))

    def append_log(self, job_id, line: str):
        with self.tx() as c:
            c.execute("INSERT INTO job_logs (job_id, ts, line) VALUES (?, ?, ?)", (job_id, now_iso(), line))
            count = c.execute("SELECT COUNT(*) FROM job_logs WHERE job_id = ?", (job_id,)).fetchone()[0]
            if count > MAX_LOG_LINES_PER_JOB:
                c.execute(
                    "DELETE FROM job_logs WHERE id IN (SELECT id FROM job_logs WHERE job_id = ? "
                    "ORDER BY id LIMIT ?)", (job_id, count - MAX_LOG_LINES_PER_JOB))

    @staticmethod
    def _decode_job(job: dict) -> dict:
        try:
            job["options"] = json.loads(job.get("options") or "{}")
        except (TypeError, json.JSONDecodeError):
            job["options"] = {}
        return job

    def get_job(self, job_id):
        row = self._row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._decode_job(row) if row else None

    def get_logs(self, job_id, after: int = 0, limit: int = 2000) -> list:
        return self._rows(
            "SELECT id, ts, line FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?",
            (job_id, after, limit))

    def list_jobs(self, status=None, parent_id=None, top_level=False, limit=50, offset=0) -> list:
        where, params = [], []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params += statuses
        if parent_id is not None:
            where.append("parent_id = ?")
            params.append(parent_id)
        elif top_level:
            where.append("parent_id IS NULL")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._rows(f"SELECT * FROM jobs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                          (*params, limit, offset))
        return [self._decode_job(r) for r in rows]

    def child_counts(self, parent_ids) -> dict:
        parent_ids = list(parent_ids)
        if not parent_ids:
            return {}
        rows = self._rows(
            f"SELECT parent_id, status, COUNT(*) AS n FROM jobs WHERE parent_id IN "
            f"({','.join('?' * len(parent_ids))}) GROUP BY parent_id, status", parent_ids)
        counts = {}
        for row in rows:
            counts.setdefault(row["parent_id"], {})[row["status"]] = row["n"]
        return counts

    def parents_with_active_children(self) -> list:
        """Finished crawl jobs whose child saves are still queued/running -
        from the user's point of view the crawl is still in progress."""
        rows = self._rows(
            """SELECT * FROM jobs WHERE id IN (
                   SELECT DISTINCT parent_id FROM jobs
                   WHERE parent_id IS NOT NULL AND status IN ('queued', 'running'))
               AND status NOT IN ('queued', 'running') ORDER BY id DESC""")
        return [self._decode_job(r) for r in rows]

    def find_active_job(self, type_, url):
        row = self._row(
            "SELECT * FROM jobs WHERE type = ? AND url = ? AND status IN ('queued', 'running') "
            "ORDER BY id LIMIT 1", (type_, url))
        return self._decode_job(row) if row else None

    def cancel_job(self, job_id) -> bool:
        """Cancels a queued job (and its queued children). A running job is
        only flagged; the worker stops it at the next checkpoint."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] not in ACTIVE_STATUSES:
                return False
            now = now_iso()
            c.execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? "
                      "WHERE (id = ? OR parent_id = ?) AND status = 'queued'", (now, now, job_id, job_id))
            return True

    def retry_job(self, job_id, overrides=None):
        """Re-run a finished job, optionally with changed options (force to
        re-save, allow_thin to keep a page judged content-free)."""
        job = self.get_job(job_id)
        if not job or job["status"] not in FINISHED_STATUSES:
            return None
        options = {**job["options"], **(overrides or {})}
        return self.create_job(job["type"], job["url"], options, job["parent_id"])

    def recover_jobs_after_restart(self) -> dict:
        """Jobs left 'running' by a crash or container restart go back in
        the queue - unless they have already been tried MAX_JOB_ATTEMPTS
        times, which points at the job itself taking the service down."""
        now = now_iso()
        with self.tx() as c:
            failed = c.execute(
                "UPDATE jobs SET status = 'failed', error = 'interrupted by a restart too many times', "
                "finished_at = ?, updated_at = ? WHERE status = 'running' AND attempts >= ?",
                (now, now, MAX_JOB_ATTEMPTS)).rowcount
            requeued = c.execute(
                "UPDATE jobs SET status = 'queued', stage = NULL, progress = NULL, progress_label = NULL, "
                "updated_at = ? WHERE status = 'running'", (now,)).rowcount
        return {"requeued": requeued, "failed": failed}

    def delete_finished_jobs(self) -> int:
        with self.tx() as c:
            ids = [r[0] for r in c.execute(
                f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(FINISHED_STATUSES))}) "
                f"AND id NOT IN (SELECT parent_id FROM jobs WHERE parent_id IS NOT NULL AND status IN "
                f"('queued', 'running'))", FINISHED_STATUSES).fetchall()]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM job_logs WHERE job_id IN ({marks})", chunk)
                c.execute(f"DELETE FROM jobs WHERE id IN ({marks})", chunk)
            return len(ids)

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------
    def stats(self, days: int = 30) -> dict:
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        since_24h = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")

        by_kind = {r["kind"] or "other": r["n"] for r in self._rows(
            "SELECT kind, COUNT(*) AS n FROM entries WHERE status = 'done' GROUP BY kind")}
        per_day_rows = {r["day"]: r["n"] for r in self._rows(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM entries "
            "WHERE status = 'done' AND created_at >= ? GROUP BY day", (start.isoformat(),))}
        per_day = [{"date": (start + timedelta(days=i)).isoformat(),
                    "count": per_day_rows.get((start + timedelta(days=i)).isoformat(), 0)}
                   for i in range(days)]

        def count_since(day) -> int:
            return self._row("SELECT COUNT(*) AS n FROM entries WHERE status = 'done' AND created_at >= ?",
                             (day.isoformat(),))["n"]

        top_areas = self._rows(
            """SELECT CASE WHEN instr(area_note, '/') > 0 THEN substr(area_note, 1, instr(area_note, '/') - 1)
                           ELSE replace(area_note, '.md', '') END AS area, COUNT(*) AS count
               FROM entries WHERE status = 'done' AND area_note IS NOT NULL
               GROUP BY area ORDER BY count DESC LIMIT 12""")
        job_counts = {r["status"]: r["n"] for r in self._rows(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running') "
            "OR finished_at >= ? GROUP BY status", (since_24h,))}
        last = self._row("SELECT processed_at FROM entries WHERE status = 'done' "
                         "ORDER BY processed_at DESC LIMIT 1")
        return {
            "entries": {
                "total": sum(by_kind.values()),
                "by_kind": by_kind,
                "today": count_since(today),
                "week": count_since(today - timedelta(days=6)),
                "month": count_since(start),
            },
            "per_day": per_day,
            "top_areas": top_areas,
            "jobs": {
                "queued": job_counts.get("queued", 0),
                "running": job_counts.get("running", 0),
                "done_24h": job_counts.get("done", 0),
                "skipped_24h": job_counts.get("skipped", 0),
                "failed_24h": job_counts.get("failed", 0),
            },
            "last_saved_at": last["processed_at"] if last else None,
        }


_db = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            from .config import settings
            _db = Database(settings.db_path)
        return _db


def set_db(db) -> None:
    """Swap the process-wide database (tests)."""
    global _db
    with _db_lock:
        _db = db
