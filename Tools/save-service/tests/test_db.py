from saver.db import Database, kind_for_note


def test_claim_complete_and_dedupe(tmp_path):
    db = Database(tmp_path / "t.db")
    state, prior = db.claim_entry("url:a", "https://a", job_id=1)
    assert (state, prior) == ("claimed", None)
    assert db.claim_entry("url:a", "https://a")[0] == "in_progress"
    db.complete_entry("url:a", url="https://a", note="Areas/x/y.md", filename="f.html",
                      area_note="x/y.md", area_entry="- #t\n\tS\n")
    state, row = db.claim_entry("url:a", "https://a")
    assert state == "exists" and row["kind"] == "page" and row["filename"] == "f.html"


def test_failed_fresh_claim_is_dropped(tmp_path):
    db = Database(tmp_path / "t.db")
    db.claim_entry("url:a", "https://a")
    db.release_claim("url:a")
    assert db.get_entry("url:a") is None


def test_failed_force_resave_keeps_previous_record(tmp_path):
    """A failed --force redo must not lose the record it was redoing."""
    db = Database(tmp_path / "t.db")
    db.complete_entry("url:a", url="https://a", note="Dumps/pdf/a.pdf", filename="a.pdf")
    state, prior = db.claim_entry("url:a", "https://a", force=True)
    assert state == "claimed" and prior["filename"] == "a.pdf"
    assert db.get_entry("url:a")["status"] == "in_progress"
    db.release_claim("url:a")
    row = db.get_entry("url:a")
    assert row["status"] == "done" and row["filename"] == "a.pdf"


def test_stale_claims_cleared(tmp_path):
    db = Database(tmp_path / "t.db")
    db.claim_entry("url:new", "https://new")
    db.complete_entry("url:old", url="https://old", note="Areas/a.md")
    db.claim_entry("url:old", "https://old", force=True)
    assert db.clear_stale_claims() == 2
    assert db.get_entry("url:new") is None
    assert db.get_entry("url:old")["status"] == "done"


def test_kind_for_note():
    assert kind_for_note("Dumps/git/github.com/a/b") == "git"
    assert kind_for_note("Dumps/youtube/Chan/Vid [x]") == "youtube"
    assert kind_for_note("Areas/hobbies/x.md") == "page"
    assert kind_for_note(None) == "other"


def test_registry_export_import_roundtrip(tmp_path):
    """Exporting the registry and importing it into an empty database must
    give back exactly the same records."""
    db = Database(tmp_path / "a.db")
    db.complete_entry("url:example.org/post", url="https://example.org/post", note="Areas/web/x.md",
                      filename="post.html", area_note="web/x.md", area_entry="- #web\n\tA post.\n")
    db.complete_entry("url:github.com/a/b", url="https://github.com/a/b", note="Dumps/git/github.com/a/b")
    db.claim_entry("url:pending", "https://pending")  # in progress: not exported
    exported = db.export_registry()
    assert set(exported) == {"url:example.org/post", "url:github.com/a/b"}

    copy = Database(tmp_path / "b.db")
    assert copy.import_registry(exported) == {"imported": 2, "skipped": 0}
    assert copy.export_registry() == exported
    assert copy.import_registry(exported) == {"imported": 0, "skipped": 2}
    stats = copy.stats()
    assert stats["entries"]["total"] == 2
    assert sum(stats["entries"]["by_kind"].values()) == 2


def test_import_canonicalizes_git_keys(tmp_path):
    """Mixed-case git-host keys in an import collapse onto the canonical key;
    a record already at the canonical key wins."""
    db = Database(tmp_path / "t.db")
    db.import_registry({
        "url:github.com/signoz/signoz": {"url": "https://github.com/signoz/signoz", "note": "Dumps/git/a",
                                         "processed_at": "2026-01-01T00:00:00"},
        "url:github.com/SigNoz/signoz": {"url": "https://github.com/SigNoz/signoz", "note": "Dumps/git/b",
                                         "processed_at": "2026-01-02T00:00:00"},
    })
    assert list(db.export_registry()) == ["url:github.com/signoz/signoz"]
    assert db.export_registry()["url:github.com/signoz/signoz"]["note"] == "Dumps/git/a"


def test_patch_entries(tmp_path):
    db = Database(tmp_path / "t.db")
    db.complete_entry("url:a", url="https://a", note="Areas/old/a.md", area_note="old/a.md", area_entry="x")
    result = db.patch_entries([
        {"key": "url:a", "set": {"note": "Areas/new/a.md", "area_note": "new/a.md"}, "unset": ["area_entry"]},
        {"key": "url:missing", "set": {"note": "Areas/z.md"}},
        {"key": "url:a", "set": {"status": "hacked"}},
    ])
    assert result == {"updated": 1, "missing": ["url:missing"], "rejected": ["url:a"]}
    row = db.get_entry("url:a")
    assert row["area_note"] == "new/a.md" and row["area_entry"] is None and row["status"] == "done"


def test_job_queue_and_recovery(tmp_path):
    db = Database(tmp_path / "t.db")
    first = db.create_job("save", "https://a", {"force": True})
    second = db.create_job("save", "https://b")
    job = db.claim_next_job()
    assert job["id"] == first and job["options"] == {"force": True} and job["attempts"] == 1
    assert db.find_active_job("save", "https://a")["id"] == first
    # simulate a crash: the running job goes back to the queue
    assert db.recover_jobs_after_restart() == {"requeued": 1, "failed": 0}
    assert db.claim_next_job()["id"] == first
    db.finish_job(first, "done", result="Saved")
    assert db.claim_next_job()["id"] == second
    assert db.cancel_job(second) is True  # running: flagged
    db.finish_job(second, "failed", error="boom")
    retry = db.retry_job(second, {"allow_thin": True})
    assert db.get_job(retry)["status"] == "queued"
    assert db.get_job(retry)["options"] == {"allow_thin": True}
    db.append_log(first, "hello")
    assert [line["line"] for line in db.get_logs(first)] == ["hello"]
    assert db.delete_finished_jobs() == 2


def test_git_keys_are_canonicalized(tmp_path):
    db = Database(tmp_path / "a.db")
    registry = {
        "url:github.com/SigNoz/signoz": {"url": "https://github.com/SigNoz/signoz", "note": "Dumps/git/github.com/SigNoz/signoz"},
        "url:github.com/signoz/signoz": {"url": "https://github.com/signoz/signoz", "note": "Dumps/git/github.com/signoz/signoz"},
        "url:github.com/Torvalds/Linux": {"url": "https://github.com/Torvalds/Linux", "note": "Dumps/git/github.com/Torvalds/Linux"},
        "url:example.com/Page": {"url": "https://example.com/Page", "note": "Areas/x.md"},
    }
    db.import_registry(registry)
    keys = set(db.export_registry())
    # the duplicate pair collapses onto the row already at the canonical key
    assert "url:github.com/signoz/signoz" in keys and "url:github.com/SigNoz/signoz" not in keys
    assert db.get_entry("url:github.com/signoz/signoz")["note"] == "Dumps/git/github.com/signoz/signoz"
    # a mixed-case key is renamed, keeping its note (the folder keeps its case)
    assert db.get_entry("url:github.com/torvalds/linux")["note"] == "Dumps/git/github.com/Torvalds/Linux"
    # other hosts keep case-sensitive paths
    assert db.has_entry("url:example.com/Page")
    assert db.canonicalize_keys() == 0  # idempotent
