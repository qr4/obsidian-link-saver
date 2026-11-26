"""HTTP API and web UI."""
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, blobs, cookies, usersettings
from .waypoints import keeper as waypoint_keeper
from .blobserve import router as blob_router
from .config import settings
from .db import ACTIVE_STATUSES, FINISHED_STATUSES, get_db, now_iso
from .inbox import InboxWatcher
from .jobctx import log
from .jobs import manager
from .urls import classify, dedupe_key
from .util import ensure_directories, sanitize_filename
from .vaultlock import vault_lock

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_EXTENSIONS = {".pdf", ".html", ".htm"}

watcher = InboxWatcher(manager)


def startup():
    ensure_directories()
    overrides = usersettings.load()
    if overrides:
        log(f"Settings changed in the web UI: {', '.join(sorted(overrides))}")
    db = get_db()
    stale = db.clear_stale_claims()
    if stale:
        log(f"Cleared {stale} stale in-progress claim(s) left by a previous run")
    manager.start(settings.workers)
    if settings.inbox_watch:
        watcher.start()
    if settings.manage_waypoints:
        waypoint_keeper.start()


def shutdown():
    waypoint_keeper.stop()
    watcher.stop()
    manager.stop()


@asynccontextmanager
async def lifespan(_app):
    startup()
    try:
        yield
    finally:
        shutdown()


def create_app(run_lifecycle: bool = True) -> FastAPI:
    app = FastAPI(title="Obsidian save service", version=__version__,
                  lifespan=lifespan if run_lifecycle else None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(blob_router)

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # Without this, browsers guess how long they may reuse the page and
        # its scripts, and keep showing the old UI for a day after an update.
        # no-cache still caches, but asks first; an unchanged file costs a 304.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def require_token(request: Request):
        # A browser on another site can still send form posts and uploads
        # here (no CORS preflight for those), riding on the missing or
        # bearer-less auth. Browsers always name the page's origin, so refuse
        # any origin that isn't this service; scripts send no Origin at all.
        origin = request.headers.get("origin")
        if origin is not None:
            own = {request.headers.get("host"), request.headers.get("x-forwarded-host"),
                   urlparse(settings.public_url).netloc}
            if urlparse(origin).netloc not in own - {None, ""}:
                raise HTTPException(status_code=403, detail="cross-site request refused")
        token = settings.api_token
        if not token:
            return
        supplied = request.headers.get("x-api-token") or request.query_params.get("token") or ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="missing or invalid API token")

    auth = [Depends(require_token)]

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    # ---- submitting work ---------------------------------------------------
    class CrawlOptions(BaseModel):
        max_depth: int = Field(3, ge=0, le=10)
        max_pages: int = Field(50, ge=1, le=5000)
        scope: str = "path"
        pdf_only: bool = False
        extra_hosts: List[str] = Field(default_factory=list, max_length=20)

    class LinksRequest(BaseModel):
        urls: List[str] = []
        url: Optional[str] = None
        force: bool = False
        allow_thin: bool = False
        crawl: Optional[CrawlOptions] = None

    @app.post("/api/links", dependencies=auth)
    def submit_links(body: LinksRequest):
        urls = list(body.urls)
        if body.url:
            urls.insert(0, body.url)
        # accept one blob of text too: split on whitespace, keep order, dedupe
        flat, seen = [], set()
        for chunk in urls:
            for url in chunk.split():
                if url not in seen:
                    seen.add(url)
                    flat.append(url)
        if not flat:
            raise HTTPException(status_code=400, detail="no URLs given")
        crawl = body.crawl.model_dump() if body.crawl else None
        results = [manager.submit_url(u, force=body.force, allow_thin=body.allow_thin, crawl_options=crawl)
                   for u in flat]
        return {"results": results}

    @app.post("/api/upload", dependencies=auth)
    async def upload(files: List[UploadFile] = File(...), source: Optional[str] = Form(None),
                     force: bool = Form(False)):
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for upload_file in files:
            name = sanitize_filename(Path(upload_file.filename or "upload").name)
            suffix = Path(name).suffix.lower()
            if suffix not in UPLOAD_EXTENSIONS:
                results.append({"file": name, "status": "invalid", "error": "only .pdf and .html files"})
                continue
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            dest = settings.upload_dir / f"{stamp}-{name}"
            with open(dest, "wb") as out:
                while chunk := await upload_file.read(1 << 20):
                    out.write(chunk)
            # a source URL only means something for a single PDF
            src = source if (source and suffix == ".pdf" and len(files) == 1) else None
            results.append(manager.submit_file(dest, source_url=src, force=force, display_name=name))
        return {"results": results}

    # ---- jobs ----------------------------------------------------------------
    def _with_children(jobs: list) -> list:
        db = get_db()
        counts = db.child_counts([j["id"] for j in jobs if j["type"] == "crawl"])
        for job in jobs:
            if job["type"] == "crawl":
                children = job["children"] = counts.get(job["id"], {})
                pending = children.get("queued", 0) + children.get("running", 0)
                total = sum(children.values())
                if pending and job["status"] not in ("queued", "running", "cancelled"):
                    # discovery is finished but pages are still being saved:
                    # present the crawl as running, with page-level progress
                    job["status"] = "running"
                    job["stage"] = "Saving crawled pages"
                    job["progress"] = (total - pending) / total if total else None
                    job["progress_label"] = f"{total - pending}/{total} pages done"
            if job["type"] != "save" or not job["url"]:
                job["kind"] = job["type"]
                continue
            # URL-based guess first; once saved, what it actually became
            # (an extension-less PDF link, a repo found by the heuristic).
            job["kind"] = classify(job["url"])
            if job["status"] == "done" and job["kind"] == "page":
                entry = db.get_entry(dedupe_key(job["url"]) or "")
                if entry and entry["kind"] in ("pdf", "git", "youtube", "video"):
                    job["kind"] = entry["kind"]
        return jobs

    @app.get("/api/jobs", dependencies=auth)
    def list_jobs(status: Optional[str] = None, parent_id: Optional[int] = None,
                  limit: int = Query(50, le=500), offset: int = 0):
        statuses = status.split(",") if status else None
        jobs = get_db().list_jobs(statuses, parent_id=parent_id, top_level=parent_id is None,
                                  limit=limit, offset=offset)
        return {"jobs": _with_children(jobs)}

    @app.get("/api/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: int, after: int = 0):
        db = get_db()
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="no such job")
        job = _with_children([job])[0]
        job["logs"] = db.get_logs(job_id, after=after)
        if job["type"] == "crawl":
            job["child_jobs"] = _with_children(db.list_jobs(parent_id=job_id, limit=500))
        return job

    @app.post("/api/jobs/{job_id}/cancel", dependencies=auth)
    def cancel_job(job_id: int):
        if not manager.cancel(job_id):
            raise HTTPException(status_code=409, detail="job is not queued or running")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry", dependencies=auth)
    def retry_job(job_id: int, force: Optional[bool] = None, allow_thin: Optional[bool] = None):
        overrides = {k: v for k, v in (("force", force), ("allow_thin", allow_thin)) if v is not None}
        new_id = get_db().retry_job(job_id, overrides)
        if not new_id:
            raise HTTPException(status_code=409, detail="only finished jobs can be retried")
        manager.wake()
        return {"job_id": new_id}

    @app.delete("/api/jobs", dependencies=auth)
    def clear_finished():
        return {"deleted": get_db().delete_finished_jobs()}

    # ---- stats / state -----------------------------------------------------------
    @app.get("/api/stats", dependencies=auth)
    def stats():
        return get_db().stats()

    @app.get("/api/state", dependencies=auth)
    def state():
        """Everything the dashboard shows, in one poll."""
        db = get_db()
        from . import transcribe
        active = _with_children(db.list_jobs(ACTIVE_STATUSES, top_level=True, limit=200)
                                + db.parents_with_active_children())
        active.sort(key=lambda j: j["id"], reverse=True)
        active_ids = {j["id"] for j in active}
        return {
            "version": __version__,
            # Timestamps are naive server-local time; the UI measures "ago"
            # against this instead of the browser's clock and time zone.
            "server_time": now_iso(),
            "paused": manager.paused,
            "workers": settings.workers,
            "stats": db.stats(),
            "active": active,
            "recent": [j for j in _with_children(db.list_jobs(FINISHED_STATUSES, top_level=True, limit=40))
                       if j["id"] not in active_ids],
            "whisper": transcribe.status(),
            "vault_lock": vault_lock.status(),
            "waypoints": waypoint_keeper.status(),
            "config": {
                "vault": str(settings.vault_dir),
                "vault_name": settings.vault_name,
                "llm_provider": settings.llm_provider,
                "llm_model": settings.llm_model,
                "cookies": cookies.status(),
                "storage": storage_status(),
                "inbox_watch": settings.inbox_watch,
            },
        }

    @app.post("/api/pause", dependencies=auth)
    def pause():
        manager.pause()
        return {"paused": True}

    @app.post("/api/resume", dependencies=auth)
    def resume():
        manager.resume()
        return {"paused": False}

    # ---- saved entries / registry -------------------------------------------------
    @app.get("/api/entries", dependencies=auth)
    def entries(q: Optional[str] = None, kind: Optional[str] = None,
                limit: int = Query(50, le=500), offset: int = 0):
        rows, total = get_db().list_entries(q=q, kind=kind, limit=limit, offset=offset)
        return {"entries": rows, "total": total}

    @app.get("/api/registry", dependencies=auth)
    def export_registry():
        """The full registry as one JSON object, keyed by entry key."""
        return get_db().export_registry()

    class RegistryPatch(BaseModel):
        key: str
        set: dict = {}
        unset: List[str] = []

    @app.post("/api/registry/patch", dependencies=auth)
    def patch_registry(patches: List[RegistryPatch]):
        return get_db().patch_entries([p.model_dump() for p in patches])

    @app.post("/api/registry/import", dependencies=auth)
    def import_registry(registry: dict, overwrite: bool = False):
        return get_db().import_registry(registry, overwrite=overwrite)

    # ---- blob store --------------------------------------------------------------------
    def storage_status() -> dict:
        status = {"enabled": blobs.enabled(), "blob_dir": settings.blob_dir or None,
                  "public_url": settings.public_url or None}
        if settings.blob_dir_inside_vault:
            status["problem"] = "BLOB_DIR is inside the vault - it would be synced back; use a separate folder"
        elif settings.blob_dir and not settings.public_url:
            status["problem"] = "BLOB_DIR is set but PUBLIC_URL is not - notes need absolute links, so the store is off"
        elif settings.blob_dir:
            try:
                usage = shutil.disk_usage(settings.blob_dir)
                status.update(free=usage.free, total=usage.total, usage=blobs.usage())
            except OSError as e:
                status["problem"] = f"blob store not accessible: {e}"
        return status

    @app.get("/api/storage", dependencies=auth)
    def get_storage():
        return storage_status()

    def _maintenance_job(type_, label, options):
        if not blobs.enabled():
            raise HTTPException(status_code=409, detail="blob store not configured (BLOB_DIR + PUBLIC_URL)")
        active = get_db().find_active_job(type_, label)
        if active:
            return {"status": "already_queued", "job_id": active["id"]}
        return {"status": "queued", "job_id": manager.enqueue(type_, label, options)}

    @app.post("/api/storage/update-repos", dependencies=auth)
    def start_update_repos():
        return _maintenance_job("update_repos", "Pull all stored repositories", {})

    # ---- waypoints -----------------------------------------------------------------
    @app.post("/api/waypoints/refresh", dependencies=auth)
    def refresh_waypoints():
        """Regenerate stale folder-note indexes now (the vault-maintenance
        skill calls this after restructuring instead of writing them itself)."""
        if not settings.manage_waypoints:
            raise HTTPException(status_code=409, detail="MANAGE_WAYPOINTS is off")
        changed = waypoint_keeper.refresh_now()
        return {"changed": [str(p.relative_to(settings.vault_dir)) for p in changed]}

    # ---- yt-dlp cookies ------------------------------------------------------------
    @app.get("/api/cookies", dependencies=auth)
    def get_cookies():
        """Counts, domains and expiry - never the cookie values."""
        return cookies.status()

    @app.post("/api/cookies", dependencies=auth)
    async def upload_cookies(file: Optional[UploadFile] = File(None), text: Optional[str] = Form(None)):
        if file is not None:
            raw = (await file.read(cookies.MAX_BYTES + 1)).decode("utf-8", errors="replace")
        else:
            raw = text or ""
        try:
            return cookies.save(raw)
        except cookies.CookieError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/cookies", dependencies=auth)
    def delete_cookies():
        return {"deleted": cookies.delete(), **cookies.status()}

    # ---- settings (Settings tab) ------------------------------------------------------
    class SettingsUpdate(BaseModel):
        values: dict = {}
        reset: List[str] = []

    class SettingsTry(BaseModel):
        values: dict = {}

    @app.get("/api/settings", dependencies=auth)
    def get_settings():
        """LLM and Whisper settings - never the API key, only whether one is set."""
        return usersettings.describe()

    @app.put("/api/settings", dependencies=auth)
    def put_settings(body: SettingsUpdate):
        try:
            return usersettings.save(body.values, body.reset)
        except usersettings.SettingsError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _candidate(values):
        try:
            return usersettings.candidate(values)
        except usersettings.SettingsError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/settings/llm/models", dependencies=auth)
    def llm_models(body: SettingsTry):
        """The models the (possibly unsaved) provider offers."""
        from . import llm
        cfg = _candidate(body.values)
        try:
            return {"models": llm.list_models(cfg), "base_url": cfg.llm_base_url}
        except (llm.LLMError, requests.RequestException, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"could not list models at {cfg.llm_base_url}: {e}")

    @app.post("/api/settings/llm/test", dependencies=auth)
    def llm_test(body: SettingsTry):
        """One tiny prompt with the (possibly unsaved) settings."""
        from . import llm
        cfg = _candidate(body.values)
        started = time.monotonic()
        try:
            reply = llm.complete('Reply with ONLY this JSON, nothing else: {"ok": true}', cfg)
        except (llm.LLMError, requests.RequestException, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"{cfg.llm_provider} at {cfg.llm_base_url}: {e}")
        return {"ok": True, "json": llm.extract_json(reply) is not None, "reply": reply.strip()[:300],
                "seconds": round(time.monotonic() - started, 1), "model": cfg.llm_model}

    # ---- vault lock leases (vault-maintenance skill) ----------------------------------
    class LeaseRequest(BaseModel):
        ttl: float = Field(600, gt=0, le=3600)
        wait: float = Field(30, ge=0, le=300)

    @app.post("/api/vault-lock", dependencies=auth)
    def acquire_lease(body: LeaseRequest = LeaseRequest()):
        lease = vault_lock.acquire_lease(body.ttl, body.wait)
        if not lease:
            return JSONResponse(status_code=409, content={"detail": "vault is locked, try again"})
        return {"lease_id": lease, "ttl": body.ttl}

    @app.post("/api/vault-lock/{lease_id}/renew", dependencies=auth)
    def renew_lease(lease_id: str, body: LeaseRequest = LeaseRequest()):
        if not vault_lock.renew_lease(lease_id, body.ttl):
            raise HTTPException(status_code=404, detail="lease expired or unknown")
        return {"lease_id": lease_id, "ttl": body.ttl}

    @app.delete("/api/vault-lock/{lease_id}", dependencies=auth)
    def release_lease(lease_id: str):
        return {"released": vault_lock.release_lease(lease_id)}

    return app


app = create_app()
