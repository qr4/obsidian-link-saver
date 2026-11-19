from urllib.parse import urlparse

from saver import crawl, jobs as jobs_mod
from saver.crawl import in_scope, relocate_into_domain_folder


def test_in_scope_rules():
    seed = urlparse("https://site.org/docs/intro")
    assert in_scope("https://site.org/guides/x", seed, "path", link_depth=1)      # first hop: any path
    assert not in_scope("https://site.org/guides/x", seed, "path", link_depth=2)  # deeper: seed prefix only
    assert in_scope("https://site.org/docs/a/b", seed, "path", link_depth=2)
    assert in_scope("https://site.org/guides/x", seed, "domain", link_depth=3)
    assert not in_scope("https://other.org/docs/a", seed, "domain", link_depth=1)
    assert in_scope("https://blog.coredump.cx/post", seed, "path", link_depth=1)
    assert not in_scope("https://site.org/logo.png", seed, "domain", link_depth=1)
    assert in_scope("https://site.org/docs/paper.pdf", seed, "path", link_depth=2)


def test_relocate_into_domain_folder(env):
    vault = env["vault"]
    page_dir = vault / "Dumps" / "single_page"
    page_dir.mkdir(parents=True)
    (page_dir / "Doc (2026).html").write_text("<html></html>")
    bullet = "- #docs\n\tDocs.\n\t[[Dumps/single_page/Doc (2026).html]]\n\thttps://site.org/docs/a\n"
    (vault / "Areas" / "programming" / "docs.md").write_text("intro\n" + bullet)
    env["db"].complete_entry("url:site.org/docs/a", url="https://site.org/docs/a", note="Areas/programming/docs.md",
                             filename="Doc (2026).html", area_note="programming/docs.md", area_entry=bullet)

    relocate_into_domain_folder("https://site.org/docs/a", "site.org")
    assert (page_dir / "site.org" / "Doc (2026).html").exists()
    entry = env["db"].get_entry("url:site.org/docs/a")
    assert entry["filename"] == "site.org/Doc (2026).html"
    note = (vault / "Areas" / "programming" / "docs.md").read_text()
    assert "[[Dumps/single_page/site.org/Doc (2026).html]]" in note and entry["area_entry"] in note
    relocate_into_domain_folder("https://site.org/docs/a", "site.org")  # idempotent
    assert entry == env["db"].get_entry("url:site.org/docs/a")


def test_crawl_enqueues_children(env, monkeypatch):
    pages = {
        "https://site.org/docs/": ["https://site.org/docs/a", "https://site.org/docs/b", "https://other.org/x",
                                   "https://site.org/img.png"],
        "https://site.org/docs/a": ["https://site.org/docs/c", "https://site.org/blog/z"],
    }
    monkeypatch.setattr(crawl, "discover_links", lambda url: pages.get(url, []))
    queued = []
    monkeypatch.setattr(jobs_mod, "enqueue", lambda t, url, opts, parent_id=None: queued.append((url, opts)) or 0)
    env["db"].complete_entry("url:site.org/docs/b", url="https://site.org/docs/b", note="Areas/a.md")
    crawl.run_crawl("https://site.org/docs/", max_depth=2, max_pages=10)
    urls = [u for u, _ in queued]
    assert urls == ["https://site.org/docs/", "https://site.org/docs/a", "https://site.org/docs/c"]
    assert queued[0][1]["relocate_domain"] == "site.org"


def test_pdf_only_crawl_stops_after_its_link_only_page_cap(env, monkeypatch):
    """HTML pages visited only to find PDFs don't count toward max_pages;
    without their own cap a big site with few PDFs is crawled for hours."""
    fetched = []

    def endless(url):
        fetched.append(url)
        n = len(fetched)
        return [f"https://site.org/docs/p{n}a", f"https://site.org/docs/p{n}b"]

    monkeypatch.setattr(crawl, "discover_links", endless)
    monkeypatch.setattr(jobs_mod, "enqueue", lambda *a, **kw: 0)
    crawl.run_crawl("https://site.org/docs/", max_depth=10, max_pages=5, pdf_only=True)
    assert len(fetched) == crawl.LINK_ONLY_PAGES_FACTOR * 5
