"""Small filesystem/text helpers."""
import errno
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .config import settings


def ensure_directories():
    """Create every base directory the service writes into or reads from
    (tree_to_string(areas_dir), etc. all assume these already exist), so a
    fresh vault doesn't crash on the first save."""
    for d in (settings.vault_dir, settings.dumps_dir, settings.page_dump_dir, settings.areas_dir,
              settings.git_dir, settings.youtube_dir, settings.video_dir, settings.pdf_dir,
              settings.singlefile_inbox_dir, settings.config_dir, settings.upload_dir):
        Path(d).mkdir(parents=True, exist_ok=True)


def create_path(file_path):
    file_path = Path(file_path)
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.touch(exist_ok=True)


def tree_to_string(start_path, prefix: str = "") -> str:
    lines = []
    entries = sorted(os.listdir(start_path))
    entries_count = len(entries)
    for i, entry in enumerate(entries):
        path = os.path.join(start_path, entry)
        connector = "└── " if i == entries_count - 1 else "├── "
        lines.append(prefix + connector + entry)
        if os.path.isdir(path):
            extension = "    " if i == entries_count - 1 else "│   "
            lines.append(tree_to_string(path, prefix + extension))
    return "\n".join(lines)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    return name or "untitled"


def safe_segment(name: str) -> str:
    """Sanitize a single path segment (e.g. a GitHub owner/repo name) so it
    can't be used to escape its intended directory via '..' or similar."""
    name = sanitize_filename(name)
    if name in ("", ".", ".."):
        return "unknown"
    return name


def derive_url_slug_title(url: str) -> str:
    """Last-resort per-page title fallback, derived from the URL's own last
    path segment. Exists for sites like theoremoftheday.org whose <title>
    tag is one static site-wide banner with no <h1>: four different pages
    collided on the identical filename "Theorem of the Day (<timestamp>)
    .html" before this existed. Used only when the chosen title still
    equals the DOM title, so a site with a genuinely fitting single-page
    <title> is unaffected."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return ""
    slug = re.sub(r"\.\w+$", "", segments[-1])
    slug = re.sub(r"[_-]+", " ", slug)
    slug = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", slug)
    return slug.strip()


def is_safe_relative_path(base_dir, rel_path: str) -> bool:
    """True if joining rel_path onto base_dir stays inside base_dir. Guards
    against a hallucinated '../..' style path from the LLM's free-form
    category suggestion writing outside the vault's subfolders."""
    base = Path(base_dir).resolve()
    candidate = (base / rel_path).resolve()
    return candidate == base or base in candidate.parents


def unique_destination(dest_dir, filename: str) -> Path:
    """Pick a filename inside dest_dir that doesn't collide with an existing
    file, atomically reserving it (O_CREAT|O_EXCL) so two concurrent saves
    landing on the same title can't both pass an exists() check for the same
    name and then race each other into overwriting one another's file.
    Callers that follow up with write_bytes/write_text, or rename a temp
    file onto the result, both happily replace this empty placeholder."""
    stem, suffix = os.path.splitext(filename)
    candidate = Path(dest_dir) / filename
    n = 2
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = Path(dest_dir) / f"{stem} ({n}){suffix}"
            n += 1


def move_file(src, dest):
    """os.replace(), falling back to copy+delete across filesystems. In the
    container /config (uploads) and /vault are separate bind mounts, and a
    rename between them fails with EXDEV even when both sit on the same disk."""
    try:
        os.replace(src, dest)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dest)
        os.unlink(src)
