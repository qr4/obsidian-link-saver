"""Site crawls.

A crawl job only does link discovery (BFS over <a href>): every in-scope
URL becomes its own child "save" job, so each page gets the exact same
treatment as one saved by hand and shows up individually in the UI. Child
jobs carry relocate_domain, so once a generic page is saved its dump moves
into Dumps/single_page/<domain>/.
"""
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import settings
from .db import get_db
from .jobctx import check_cancelled, current, log, progress, result, stage
from .urls import dedupe_key
from .vaultlock import vault_lock

FETCH_TIMEOUT = 30
CRAWLER_UA = "Mozilla/5.0 (compatible; obsidian-save-service crawler)"

# The crawl is same-host only; these are hand-listed exceptions, applied
# only to first-level links (never crawled onward). lcamtuf.coredump.cx/
# offsite.shtml indexes writing that lives on blog.coredump.cx.
CROSS_HOST_ALLOWED = {"blog.coredump.cx"}


def looks_like_page_or_pdf(url: str) -> bool:
    """A clean route with no extension, an HTML-ish extension (including
    server-rendered spellings), or a direct .pdf link."""
    last_segment = urlparse(url).path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return True
    ext = last_segment.rsplit(".", 1)[-1].lower()
    return ext in ("html", "htm", "shtml", "xhtml", "php", "asp", "aspx", "jsp", "pdf")


def is_pdf_link(url: str) -> bool:
    """Extension-only check for deciding whether to run link discovery -
    not the HEAD-requesting is_pdf_url, which would cost a round trip per
    discovered link."""
    return urlparse(url).path.lower().endswith(".pdf")


def in_scope(url: str, seed_parsed, scope: str, link_depth: int = 2) -> bool:
    """First-level links (found on the seed page) are followed whatever
    their path - a seed routinely links siblings one level up. From the
    second hop on the seed's path prefix is enforced (scope="path"), so one
    off-prefix link can't snowball into crawling the whole site. Host is
    stricter: same host at every depth, CROSS_HOST_ALLOWED at depth 1 only."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not looks_like_page_or_pdf(url):
        return False
    if parsed.netloc != seed_parsed.netloc:
        return link_depth <= 1 and parsed.netloc in CROSS_HOST_ALLOWED
    if scope == "path" and link_depth > 1:
        seed_dir = seed_parsed.path.rsplit("/", 1)[0] + "/"
        return parsed.path.startswith(seed_dir)
    return True


def discover_links(url: str) -> list:
    if is_pdf_link(url):
        return []
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": CRAWLER_UA})
        response.raise_for_status()
    except requests.RequestException as e:
        log(f"WARNING: could not fetch {url} for link discovery ({e})")
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(url, a["href"]).split("#")[0]
        if absolute:
            links.append(absolute)
    return links


def relocate_into_domain_folder(url: str, domain: str):
    """Move a crawled generic page's dump from the flat Dumps/single_page/
    into Dumps/single_page/<domain>/, and patch everything that remembers
    the old path: the [[wikilink]] in the Area bullet and the entry's
    filename/area_entry (which force re-saves rely on).

    Whether to act is decided from the database ("does it still name a flat
    path?"), not the filesystem, so a half-applied move heals on a re-run
    instead of freezing. Move and pointer updates happen under the vault
    lock so they are never observably out of step."""
    key = dedupe_key(url)
    if not key:
        return
    db = get_db()
    page_dir = Path(settings.page_dump_dir)
    with vault_lock():
        entry = db.get_entry(key)
        if not entry or entry["status"] != "done" or entry["kind"] != "page" or not entry.get("filename"):
            return
        old_filename = entry["filename"]
        if "/" in old_filename:
            return  # already carries its domain folder

        new_filename = f"{domain}/{old_filename}"
        old_path = page_dir / old_filename
        new_path = page_dir / new_filename
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
        elif not new_path.exists():
            return  # in neither place - leave the stale pointer rather than guess

        area_note = entry.get("area_note")
        area_entry = entry.get("area_entry")
        old_link = f"[[Dumps/single_page/{old_filename}]]"
        new_link = f"[[Dumps/single_page/{new_filename}]]"
        new_area_entry = area_entry
        if area_note and area_entry and old_link in area_entry:
            area_path = Path(settings.areas_dir) / area_note
            if area_path.exists():
                text = area_path.read_text(encoding="utf-8")
                if area_entry in text:
                    new_area_entry = area_entry.replace(old_link, new_link)
                    area_path.write_text(text.replace(area_entry, new_area_entry, 1), encoding="utf-8")
                elif old_link in text:
                    # the stored entry drifted from the note - patch the link alone
                    new_area_entry = area_entry.replace(old_link, new_link)
                    area_path.write_text(text.replace(old_link, new_link), encoding="utf-8")

        db.update_entry_fields(key, filename=new_filename, area_entry=new_area_entry)
    log(f"Relocated -> Dumps/single_page/{new_filename}")


def run_crawl(seed_url: str, max_depth: int = 3, max_pages: int = 50, scope: str = "path",
              force: bool = False, pdf_only: bool = False, allow_thin: bool = False):
    from .jobs import enqueue  # late import: jobs imports this module

    reporter = current()
    parent_id = reporter.job_id if reporter else None
    seed_parsed = urlparse(seed_url)
    seen = {seed_url}
    queue = deque([(seed_url, 0)])
    queued = skipped = 0
    db = get_db()
    stage("Discovering pages")

    while queue and (queued + skipped) < max_pages:
        check_cancelled()
        url, depth = queue.popleft()

        # pdf_only: HTML pages are pure link-discovery hops toward the PDFs
        # they link to, never saved themselves.
        if pdf_only and not is_pdf_link(url):
            log(f"[depth {depth}] {url} (not a PDF, link discovery only)")
        elif not force and db.has_entry(dedupe_key(url) or ""):
            skipped += 1
            log(f"[depth {depth}] {url}: already saved")
        else:
            queued += 1
            enqueue("save", url, {"force": force, "allow_thin": allow_thin,
                                  "relocate_domain": seed_parsed.netloc}, parent_id=parent_id)
            log(f"[depth {depth}, {queued + skipped}/{max_pages}] {url}: queued")
        progress((queued + skipped) / max_pages,
                 f"{queued} queued · {skipped} already saved · {len(queue)} in frontier")

        if depth >= max_depth:
            continue
        for link in discover_links(url):
            if link not in seen and in_scope(link, seed_parsed, scope, depth + 1):
                seen.add(link)
                queue.append((link, depth + 1))

    result(f"Crawl queued {queued} page(s), {skipped} already saved "
           f"({len(seen)} URLs discovered in scope, capped at {max_pages})")
