"""The save pipeline end to end against a throwaway vault, with page
capture and the LLM stubbed."""
from io import BytesIO

import pytest
from pypdf import PdfWriter

from saver import fetch, pipeline as saver_mod
from saver.errors import ContentFreeError
from saver.pipeline import Saver

PAGE = """<!DOCTYPE html><html><!--
 Page saved with SingleFile
 url: https://blog.example.com/posts/scraping
 saved date: today
--><head><title>Blog</title></head><body><h1>Scraping</h1><p>How to scrape politely.</p></body></html>"""


@pytest.fixture
def capture(monkeypatch):
    calls = []

    def fake_single_file(url, dest_dir, filename):
        calls.append(url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / filename
        path.write_text(PAGE.replace("https://blog.example.com/posts/scraping", url), encoding="utf-8")
        return path

    monkeypatch.setattr(saver_mod, "save_single_file", fake_single_file)
    monkeypatch.setattr(saver_mod, "fetch_page_html", lambda url: PAGE)
    monkeypatch.setattr(saver_mod, "is_pdf_url", lambda url: url.endswith(".pdf"))
    return calls


def area_text(env, rel="programming/web-scraping.md"):
    return (env["vault"] / "Areas" / rel).read_text(encoding="utf-8")


def test_generic_page_save_and_dedupe(env, capture):
    url = "https://blog.example.com/posts/scraping"
    assert Saver().save(url) == "consumed"

    entry = env["db"].get_entry("url:blog.example.com/posts/scraping")
    assert entry["status"] == "done" and entry["kind"] == "page"
    assert entry["note"] == "Areas/programming/web-scraping.md"
    dump = env["vault"] / "Dumps" / "single_page" / entry["filename"]
    assert dump.exists() and entry["filename"].startswith("Scraping Guide (")
    text = area_text(env)
    assert text == entry["area_entry"]
    assert text.startswith("- #web #scraping\n\tA page about scraping.\n\t[[Dumps/single_page/")
    assert text.rstrip().endswith(url)
    # no temp capture left behind
    assert not list((env["vault"] / "Dumps" / "single_page").glob("_capture-*"))
    # the new top-level folder got a graph color group
    assert "Areas/programming" in (env["vault"] / ".obsidian" / "graph.json").read_text()

    assert Saver().save(url) == "skipped_dedupe"
    assert len(capture) == 1


def test_force_resave_replaces_bullet_and_file(env, capture):
    url = "https://blog.example.com/posts/scraping"
    Saver().save(url)
    first = env["db"].get_entry("url:blog.example.com/posts/scraping")
    env["llm"]["summary"] = "An updated summary."
    Saver().save(url, force=True)
    second = env["db"].get_entry("url:blog.example.com/posts/scraping")
    assert second["filename"] == first["filename"]  # overwritten in place, not orphaned
    text = area_text(env)
    assert text.count("- #web") == 1 and "An updated summary." in text
    assert len(list((env["vault"] / "Dumps" / "single_page").iterdir())) == 1


def test_failed_force_resave_keeps_record(env, capture, monkeypatch):
    url = "https://blog.example.com/posts/scraping"
    Saver().save(url)
    before = env["db"].get_entry("url:blog.example.com/posts/scraping")

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(saver_mod.llm, "extract_tags", boom)
    with pytest.raises(RuntimeError):
        Saver().save(url, force=True)
    after = env["db"].get_entry("url:blog.example.com/posts/scraping")
    assert after["status"] == "done" and after["area_entry"] == before["area_entry"]


def test_content_free_page_is_rejected_cleanly(env, capture):
    env["llm"]["has_content"] = False
    with pytest.raises(ContentFreeError):
        Saver().save("https://blog.example.com/tag/python")
    assert env["db"].get_entry("url:blog.example.com/tag/python") is None
    assert not list((env["vault"] / "Dumps" / "single_page").glob("*.html"))
    # allow_thin keeps it
    assert Saver().save("https://blog.example.com/tag/python", allow_thin=True) == "consumed"


def test_unsafe_area_falls_back_to_uncategorized(env, capture):
    env["llm"]["area"] = "../../etc/passwd.md"
    Saver().save("https://blog.example.com/a")
    assert (env["vault"] / "Areas" / "uncategorized.md").exists()
    env["llm"]["area"] = "hobbies/hobbies.md"
    Saver().save("https://blog.example.com/b")
    assert not (env["vault"] / "Areas" / "hobbies" / "hobbies.md").exists()


def test_import_captured_file(env, capture, tmp_path):
    inbox = env["vault"] / "Dumps" / "singlefile_inbox"
    inbox.mkdir(parents=True)
    path = inbox / "Blog (today).html"
    path.write_text(PAGE, encoding="utf-8")
    assert Saver().import_captured_file(path) == "consumed"
    assert not path.exists()  # renamed into the dump
    assert capture == []  # no re-capture

    path.write_text(PAGE, encoding="utf-8")
    assert Saver().import_captured_file(path) == "skipped_dedupe"
    assert not path.exists()  # a duplicate is dropped, not parked
    assert not (inbox / "_duplicate").exists()


def make_pdf(title="A Paper"):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title, "/Subject": "Sheet music for piano solo"})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_import_pdf_without_text_layer(env, tmp_path):
    upload = tmp_path / "scan.pdf"
    upload.write_bytes(make_pdf())
    assert Saver().import_pdf_file(upload) == "dispatched_other"
    assert not upload.exists()
    stored = env["vault"] / "Dumps" / "pdf" / "A Paper.pdf"
    assert stored.exists()
    rows, total = env["db"].list_entries(kind="pdf")
    assert total == 1 and rows[0]["url"].startswith("local://pdf-import/") and rows[0]["filename"] == "A Paper.pdf"

    # byte-identical re-upload is dropped, not saved twice
    again = tmp_path / "copy.pdf"
    again.write_bytes(make_pdf())
    assert Saver().import_pdf_file(again) == "skipped_dedupe"
    assert not again.exists() and not (tmp_path / "_duplicate").exists()
    assert stored.exists()


def test_not_a_pdf(env, tmp_path):
    bogus = tmp_path / "x.pdf"
    bogus.write_text("<html>error</html>")
    with pytest.raises(Exception, match="not a PDF"):
        Saver().import_pdf_file(bogus)


def test_singlefile_url_header():
    assert fetch.extract_singlefile_source_url(PAGE) == "https://blog.example.com/posts/scraping"
    assert fetch.extract_singlefile_source_url("<html></html>") == ""


def test_upload_moves_across_filesystems(env, capture, monkeypatch):
    """Uploads live in /config, dumps in /vault: separate mounts in Docker."""
    import errno
    import os
    from saver import util
    from saver.config import settings

    real_replace = os.replace

    def cross_device(src, dest):
        if str(settings.upload_dir) in str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dest)

    monkeypatch.setattr(util.os, "replace", cross_device)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    upload = settings.upload_dir / "20261107-page.html"
    upload.write_text(PAGE, encoding="utf-8")
    assert Saver().import_captured_file(upload) == "consumed"
    assert not upload.exists()
    entry = env["db"].get_entry("url:blog.example.com/posts/scraping")
    assert (env["vault"] / "Dumps" / "single_page" / entry["filename"]).read_text(encoding="utf-8") == PAGE

    # a duplicate upload is deleted
    upload.write_text(PAGE, encoding="utf-8")
    assert Saver().import_captured_file(upload) == "skipped_dedupe"
    assert not upload.exists() and not (settings.upload_dir / "_duplicate").exists()


def test_repo_check_skips_the_llm_for_ordinary_pages(env, capture, monkeypatch):
    asked = []
    monkeypatch.setattr(saver_mod.llm, "detect_git_repo_prompt", lambda *a: asked.append(a) or {})
    assert Saver().detect_git_repo("https://blog.example.com/post", PAGE) == (None, None, None)
    assert asked == []


def test_repo_check_asks_the_llm_on_forge_pages(env, monkeypatch):
    asked = []
    monkeypatch.setattr(saver_mod.llm, "detect_git_repo_prompt", lambda *a: asked.append(a) or {"is_repo": False})
    page = "<html><head><title>tool</title></head><body>Powered by Forgejo. " \
           "git clone https://code.example.org/me/tool.git</body></html>"
    assert Saver().detect_git_repo("https://code.example.org/me/tool", page) == (None, None, None)
    assert len(asked) == 1


def test_failure_after_capture_leaves_no_temp_file(env, capture, monkeypatch):
    def timeout(_text):
        raise TimeoutError("Ollama timed out")
    monkeypatch.setattr(saver_mod.llm, "extract_title", timeout)
    with pytest.raises(TimeoutError):
        Saver().save("https://blog.example.com/posts/slow")
    assert capture  # the page was captured before the failure
    assert not list((env["vault"] / "Dumps" / "single_page").glob("_capture-*"))


def test_long_text_is_trimmed_before_the_llm(env, monkeypatch):
    """A very long page would overflow the model's context window, and Ollama
    cuts the prompt silently; tags and summary read only the start."""
    seen = []
    monkeypatch.setattr(saver_mod.llm, "extract_tags", lambda text: seen.append(len(text)) or ["t"])
    monkeypatch.setattr(saver_mod.llm, "extract_summary",
                        lambda text, require_content=True: seen.append(len(text)) or "s")
    Saver().analyze("word " * 100_000)
    assert seen == [saver_mod.ANALYZE_CHARS] * 2


def test_captures_drop_cookie_banners(monkeypatch, tmp_path):
    """The server's Chromium has no banner blocker: SingleFile removes the
    consent tools' banners itself and undoes their scroll lock."""
    from types import SimpleNamespace

    from saver.config import settings

    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        (tmp_path / "p.html").write_text("<html></html>")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fetch.proc, "run", fake_run)
    cookie_css = tmp_path / "common.css"
    cookie_css.write_text("#banner { display: none !important }")
    monkeypatch.setattr(settings, "singlefile_cookie_css", str(cookie_css))
    fetch.save_single_file("https://example.com", tmp_path, "p.html")
    cmd = calls[-1]
    assert "#onetrust-consent-sdk" in cmd[cmd.index("--removed-elements-selector") + 1]
    sheets = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--browser-stylesheet"]
    # the banner rules first, then the scroll fix that overrides them
    assert sheets[0] == str(cookie_css) and "overflow-y: auto" in open(sheets[1]).read()

    # outside the image the rules are missing: captures still work without them
    monkeypatch.setattr(settings, "singlefile_cookie_css", str(tmp_path / "missing.css"))
    fetch.save_single_file("https://example.com", tmp_path, "p.html")
    assert calls[-1].count("--browser-stylesheet") == 1

    monkeypatch.setattr(settings, "singlefile_removed_elements", "none")
    fetch.save_single_file("https://example.com", tmp_path, "p.html")
    assert "--removed-elements-selector" not in calls[-1] and "--browser-stylesheet" not in calls[-1]
