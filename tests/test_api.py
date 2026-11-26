"""HTTP API + job workers, with the save pipeline stubbed."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from saver import jobs as jobs_mod
from saver.app import create_app
from saver.config import settings
from saver.jobctx import library_entry, log, progress, result, stage
from saver.vaultlock import vault_lock


@pytest.fixture
def client(env, monkeypatch):
    def fake_save(self, url, force=False, allow_thin=False, **kw):
        stage("Capturing page")
        progress(0.5, "halfway")
        log("doing work")
        if "fail" in url:
            raise RuntimeError("simulated failure")
        key = jobs_mod.dedupe_key(url)
        state, _ = env["db"].claim_entry(key, url, force=force)
        if state != "claimed":
            result("Already saved")
            return "skipped_dedupe"
        env["db"].complete_entry(key, url=url, note="Areas/x/y.md", area_note="x/y.md", area_entry="- e\n")
        result("Saved -> Areas/x/y.md")
        return "consumed"

    monkeypatch.setattr(jobs_mod.Saver, "save", fake_save)
    manager = jobs_mod.manager
    manager.start(1)
    with TestClient(create_app(run_lifecycle=False)) as c:
        yield c
    manager.stop()


def wait_for(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_submit_runs_and_dedupes(client):
    response = client.post("/api/links", json={"urls": ["https://example.com/a\nhttps://example.com/fail  not-a-url"]})
    results = response.json()["results"]
    assert [r["status"] for r in results] == ["queued", "queued", "invalid"]

    done = wait_for(client, results[0]["job_id"])
    assert done["status"] == "done" and done["result"] == "Saved -> Areas/x/y.md" and done["progress"] == 1.0
    assert any(line["line"] == "» Capturing page" for line in done["logs"])
    failed = wait_for(client, results[1]["job_id"])
    assert failed["status"] == "failed" and "simulated failure" in failed["error"]

    again = client.post("/api/links", json={"url": "https://www.example.com/a/"}).json()["results"][0]
    assert again["status"] == "already_saved" and again["note"] == "Areas/x/y.md"

    retry = client.post(f"/api/jobs/{results[1]['job_id']}/retry").json()
    assert wait_for(client, retry["job_id"])["status"] == "failed"

    state = client.get("/api/state").json()
    assert state["stats"]["entries"]["total"] == 1
    assert state["stats"]["jobs"]["failed_24h"] == 2
    assert {j["status"] for j in state["recent"]} == {"done", "failed"}
    assert client.get("/api/entries", params={"q": "example"}).json()["total"] == 1


def test_pause_and_cancel(client):
    client.post("/api/pause")
    job_id = client.post("/api/links", json={"url": "https://example.com/p"}).json()["results"][0]["job_id"]
    time.sleep(0.3)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
    client.post("/api/resume")
    assert client.get("/api/state").json()["paused"] is False


def test_pause_survives_restart(client, env):
    manager = jobs_mod.manager
    client.post("/api/pause")
    manager.stop()
    manager.start(1)
    assert manager.paused is True
    client.post("/api/resume")
    assert env["db"].get_meta("paused") == "0"


def test_registry_export_and_patch(client, env):
    env["db"].complete_entry("url:a", url="https://a", note="Areas/old/a.md", area_note="old/a.md", area_entry="x")
    registry = client.get("/api/registry").json()
    assert registry["url:a"]["area_note"] == "old/a.md"
    patched = client.post("/api/registry/patch", json=[
        {"key": "url:a", "set": {"area_note": "new/a.md", "note": "Areas/new/a.md"}, "unset": ["area_entry"]}]).json()
    assert patched["updated"] == 1
    assert client.get("/api/registry").json()["url:a"] == {
        "url": "https://a", "note": "Areas/new/a.md", "processed_at": registry["url:a"]["processed_at"],
        "area_note": "new/a.md"}


def test_upload_queues_import(client, env, monkeypatch):
    imported = []
    monkeypatch.setattr(jobs_mod.Saver, "import_captured_file", lambda self, p: imported.append(p.name) or "consumed")
    files = [("files", ("page.html", b"<html></html>", "text/html")),
             ("files", ("evil.exe", b"MZ", "application/octet-stream"))]
    results = client.post("/api/upload", files=files).json()["results"]
    assert results[1]["status"] == "invalid"
    assert wait_for(client, results[0]["job_id"])["status"] == "done"
    assert imported and imported[0].endswith("-page.html")


def test_import_job_records_its_library_entry(client, monkeypatch):
    """An upload is labelled with the file's name; the job keeps what finds
    the entry it became, so the UI can link it to the library."""
    def fake_import(self, path):
        library_entry("https://blog.example.com/a")
        return "consumed"

    monkeypatch.setattr(jobs_mod.Saver, "import_captured_file", fake_import)
    files = [("files", ("page.html", b"<html></html>", "text/html"))]
    job_id = client.post("/api/upload", files=files).json()["results"][0]["job_id"]
    job = wait_for(client, job_id)
    assert job["status"] == "done" and job["options"]["library_q"] == "https://blog.example.com/a"


def test_tabs_have_their_own_paths(client):
    page = client.get("/").text
    for path in ("/library", "/settings", "/library?q=x&kind=pdf"):
        response = client.get(path)
        assert response.status_code == 200 and response.text == page


def test_token_required(client):
    settings.update(api_token="s3cret")
    try:
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
    finally:
        settings.update(api_token="")


def test_vault_lease_blocks_writers(client):
    lease = client.post("/api/vault-lock", json={"ttl": 30, "wait": 1}).json()["lease_id"]
    assert client.post("/api/vault-lock", json={"ttl": 30, "wait": 0}).status_code == 409
    entered = threading.Event()

    def writer():
        with vault_lock():
            entered.set()

    thread = threading.Thread(target=writer)
    thread.start()
    assert not entered.wait(0.5)  # blocked while the lease is held
    assert client.delete(f"/api/vault-lock/{lease}").json() == {"released": True}
    assert entered.wait(3)
    thread.join()


def test_crawl_with_running_children_stays_active(client, env):
    db = env["db"]
    client.post("/api/pause")
    crawl_id = db.create_job("crawl", "https://site.org/", {"max_pages": 3})
    db.claim_next_job()
    db.finish_job(crawl_id, "done", result="Crawl queued 2 page(s)")
    db.create_job("save", "https://site.org/a", {}, parent_id=crawl_id)
    child = db.create_job("save", "https://site.org/b", {}, parent_id=crawl_id)
    db.finish_job(child, "done")
    state = client.get("/api/state").json()
    crawl = next(j for j in state["active"] if j["id"] == crawl_id)
    assert crawl["status"] == "running" and crawl["progress"] == 0.5 and crawl["progress_label"] == "1/2 pages done"
    assert all(j["id"] != crawl_id for j in state["recent"])
    client.post("/api/resume")


def test_cross_site_requests_are_refused(client):
    # a form post from another site carries that site's Origin
    assert client.post("/api/pause", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/state").json()["paused"] is False
    # the web UI itself (same origin) and scripts (no Origin) still work
    assert client.post("/api/pause", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/resume").status_code == 200
    # behind a proxy that rewrites Host, the forwarded host counts as our own
    assert client.post("/api/resume", headers={"Origin": "https://saver.example.com",
                                               "X-Forwarded-Host": "saver.example.com"}).status_code == 200


def test_ui_is_revalidated_after_updates(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    script = client.get("/static/app.js")
    assert script.headers["cache-control"] == "no-cache"
    # an unchanged script costs only a 304 on the next load
    assert client.get("/static/app.js", headers={"If-None-Match": script.headers["etag"]}).status_code == 304
    assert "cache-control" not in client.get("/api/health").headers
