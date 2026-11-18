"""Fetching pages and PDFs, PDF text helpers, and SingleFile capture."""
import hashlib
import json
import re
import subprocess
from io import BytesIO
from pathlib import Path

import requests
from pypdf import PdfReader

from . import proc
from .config import settings
from .util import safe_segment, sanitize_filename


def _headers():
    return {"User-Agent": settings.http_user_agent}


def fetch_page_html(url: str) -> str:
    response = requests.get(url, timeout=30, headers=_headers())
    response.raise_for_status()
    return response.text


def fetch_pdf(url: str) -> bytes:
    response = requests.get(url, timeout=60, headers=_headers())
    response.raise_for_status()
    return response.content


PDF_TEXT_MAX_PAGES = 20  # plenty for tagging/summarizing without stalling on huge documents
PDF_TEXT_MIN_CHARS = 40  # below this there is nothing for the model to read


def extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    pages = reader.pages[:PDF_TEXT_MAX_PAGES]
    return "\n".join(page.extract_text() or "" for page in pages)


def extract_pdf_metadata_title(data: bytes) -> str:
    """The PDF's embedded /Title, if any. LaTeX-produced PDFs (arXiv papers
    etc.) routinely leave this blank, so callers need a fallback."""
    try:
        reader = PdfReader(BytesIO(data))
        return (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        return ""


def pdf_metadata_digest(data: bytes) -> str:
    """The PDF's own /Title, /Subject, /Author and /Keywords as a text block.

    Used when a PDF has no text layer (a scan, an image-to-PDF export):
    tagging from the handful of newlines extract_pdf_text returns yields
    whatever the model invents. The metadata is often the only genuine
    description such a file carries."""
    try:
        meta = PdfReader(BytesIO(data)).metadata or {}
    except Exception:
        return ""
    fields = [("Title", meta.get("/Title")), ("Subject", meta.get("/Subject")),
              ("Author", meta.get("/Author")), ("Keywords", meta.get("/Keywords"))]
    return "\n".join(f"{label}: {str(value).strip()}" for label, value in fields if str(value or "").strip())


def find_identical_pdf(data: bytes, ignoring: Path = None):
    """An existing pdf_dir file holding exactly these bytes, or None. The
    registry is keyed on where something came from and can't see that a
    hand-downloaded paper is one the vault already has under its
    publisher's URL. Sizes are compared before hashing, so the usual answer
    costs a few dozen stat() calls."""
    pdf_dir = Path(settings.pdf_dir)
    if not pdf_dir.is_dir():
        return None
    ignoring = ignoring.resolve() if ignoring else None
    digest = None
    for candidate in sorted(pdf_dir.glob("*.pdf")):
        try:
            if candidate.stat().st_size != len(data) or candidate.resolve() == ignoring:
                continue
            digest = digest or hashlib.sha256(data).hexdigest()
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                return candidate
        except OSError:
            continue
    return None


def pdf_page_count(data: bytes) -> str:
    """Page count as a string, or "?" for a PDF too damaged to report one -
    only ever used as prose in a prompt."""
    try:
        return str(len(PdfReader(BytesIO(data)).pages))
    except Exception:
        return "?"


def looks_like_pdf(data: bytes) -> bool:
    """The %PDF- magic number. Guards raw-file imports against a truncated
    download or an HTML error page saved under a .pdf name."""
    return data[:5] == b"%PDF-"


def pdf_import_source_url(data: bytes, path: Path) -> str:
    """A synthesized stand-in source for a raw PDF with no known origin:

      local://pdf-import/<sha256[:16]>/<slug>.pdf

    The digest is over the file's bytes, so the same PDF re-uploaded under a
    different name still dedupes. local:// is deliberately not http(s), so
    nothing mistakes it for a fetchable address."""
    digest = hashlib.sha256(data).hexdigest()[:16]
    slug = safe_segment(sanitize_filename(path.stem)[:120]) or "untitled"
    return f"local://pdf-import/{digest}/{slug}.pdf"


def save_single_file(url: str, dest_dir: Path, filename: str) -> Path:
    """Render the page with the SingleFile CLI and the bundled Chromium into
    dest_dir/filename: a full-fidelity, JS-rendered, self-contained snapshot.

    `filename` must be unique already - SingleFile does not overwrite an
    existing file by default but silently writes "name (2).html" next to it,
    so callers capture to a timestamped temp name and rename afterwards.
    Chromium needs --no-sandbox inside a container."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    cmd = [
        settings.singlefile_bin, url, str(dest),
        "--browser-executable-path", settings.chromium_bin,
        "--browser-args", json.dumps(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]),
        "--filename-conflict-action", "overwrite",
    ]
    try:
        result = proc.run(cmd, timeout=settings.singlefile_timeout)
    except FileNotFoundError:
        raise RuntimeError(f"SingleFile CLI not found ({settings.singlefile_bin})")
    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"SingleFile timed out after {settings.singlefile_timeout}s")
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError((result.stderr or result.stdout).strip()[-1000:] or "SingleFile produced no output file")
    return dest


def extract_singlefile_source_url(html: str) -> str:
    """SingleFile inserts a leading HTML comment - 'Page saved with
    SingleFile / url: <url> / saved date: ...' - as the first child of
    <html>. That comment is the only record of which page a manually
    captured file came from."""
    comment_match = re.search(r"<!--(.*?)-->", html[:4096], flags=re.DOTALL)
    if not comment_match:
        return ""
    url_match = re.search(r"^\s*url:\s*(\S+)\s*$", comment_match.group(1), flags=re.MULTILINE)
    return url_match.group(1) if url_match else ""
