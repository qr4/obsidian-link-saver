"""Offsite storage for the bulky dumps: full git clones and video files.

With BLOB_DIR + PUBLIC_URL set, downloads and clones are written to the
blob store (a folder outside the vault), served by this service
under /blobs and /watch, and the vault keeps only what is small and worth
searching:

  Dumps/git/<host>/<owner>/<repo>/   root README(s) - whole gists - and
                                     .offloaded (URL of the clone's listing)
  Dumps/youtube|video/.../<title>/   transcripts and subtitles (out.srt,
                                     out.vtt, out.txt, out.json, yt-dlp's
                                     .srt) - and "<video stem>.offloaded"
                                     (the video's watch URL), plus
                                     .offloaded when it's the folder's only
                                     video

The store mirrors the Dumps/ layout (<store>/<category>/<path>), so the
offload job can move what saves made without a store left in the vault, and
the two never disagree about where a file goes.
"""
import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import proc
from .config import settings
from .jobctx import check_cancelled, log, progress, result, stage
from .util import write_text_atomic
from .vaultlock import vault_lock

CATEGORIES = ("git", "youtube", "video")
# Folder marker: a repo's listing URL, or the watch URL when the folder
# holds exactly one video (the usual "<title> [id]/" layout).
MARKER = ".offloaded"
# Per-video marker "<video stem>.offloaded", needed where many videos share
# a folder (channel saves); readers try it first, then the folder marker.
MARKER_SUFFIX = ".offloaded"
VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
TEMP_SUFFIXES = (".part", ".ytdl", ".subs-tmp.mkv", ".temp", ".tmp")
README_NAMES = ("README.md", "Readme.md", "readme.md", "README.MD", "README.rst",
                "README.org", "readme.org", "README.txt", "README")
SIDECAR_MAX_BYTES = 50 * 1024 * 1024
GIST_HOST = "gist.github.com"


def enabled() -> bool:
    return settings.blobs_enabled


def store_root() -> Path:
    return Path(settings.blob_root)


def vault_dir(category: str, rel: str) -> Path:
    return settings.dumps_dir / category / rel


def blob_dir(category: str, rel: str) -> Path:
    return store_root() / category / rel


def _quoted(category: str, rel: str) -> str:
    return quote(f"{category}/{rel}".strip("/"), safe="/")


def url(category: str, rel: str, directory: bool = False) -> str:
    return f"{settings.public_url}/blobs/{_quoted(category, rel)}" + ("/" if directory else "")


def watch_url(category: str, rel_file: str) -> str:
    return f"{settings.public_url}/watch/{_quoted(category, rel_file)}"


def _is_media(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def _has_media(directory: Path) -> bool:
    return directory.is_dir() and any(p.is_file() and _is_media(p) for p in directory.iterdir())


# ---------------------------------------------------------------------------
# videos
# ---------------------------------------------------------------------------
def media_work_dir(category: str, rel_dir: str) -> Path:
    """Where a video download should land. A video an older save left in
    the vault is reused there (`python -m saver offload` moves it) instead of
    being downloaded a second time into the store."""
    if not enabled():
        return vault_dir(category, rel_dir)
    in_vault = vault_dir(category, rel_dir)
    if _has_media(in_vault):
        log(f"This video is still in the vault ({in_vault}); working there - run `python -m saver offload` to move it")
        return in_vault
    return blob_dir(category, rel_dir)


def write_video_markers(vault_folder: Path, category: str, rel_file: str, only_video_in_folder: bool):
    """Point the vault folder at a video that now lives in the store."""
    watch = watch_url(category, rel_file) + "\n"
    vault_folder.mkdir(parents=True, exist_ok=True)
    (vault_folder / (Path(rel_file).stem + MARKER_SUFFIX)).write_text(watch, encoding="utf-8")
    if only_video_in_folder:
        (vault_folder / MARKER).write_text(watch, encoding="utf-8")


def _sidecars(directory: Path):
    for path in sorted(directory.iterdir()):
        if (path.is_file() and not path.is_symlink() and not _is_media(path)
                and not path.name.endswith(MARKER_SUFFIX)
                and not path.name.endswith(TEMP_SUFFIXES) and ".part-Frag" not in path.name
                and path.stat().st_size <= SIDECAR_MAX_BYTES):
            yield path


def mirror_video(category: str, rel_dir: str, media_file: Path):
    """Copy a stored video's transcripts/subtitles into the vault folder and
    drop the .offloaded marker pointing at its watch page. No-op when the
    video already lives in the vault."""
    if not enabled() or vault_dir(category, rel_dir) == media_file.parent:
        return
    target = vault_dir(category, rel_dir)
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for sidecar in _sidecars(media_file.parent):
        shutil.copy2(sidecar, target / sidecar.name)
        copied.append(sidecar.name)
    videos = [p for p in media_file.parent.iterdir() if p.is_file() and _is_media(p)]
    write_video_markers(target, category, f"{rel_dir}/{media_file.name}", only_video_in_folder=len(videos) == 1)
    log(f"Kept in the vault: {', '.join(copied) or 'nothing but the marker'}")


def transcript_link(category: str, rel_dir: str, media_name: str):
    """Vault path of the best transcript next to a video, for the note link."""
    folder = vault_dir(category, rel_dir)
    stem = Path(media_name).stem
    for name in ("out.txt", f"{stem}.txt", "out.srt", f"{stem}.srt"):
        if (folder / name).is_file():
            return f"Dumps/{category}/{rel_dir}/{name}"
    if folder.is_dir():
        for path in sorted(folder.glob(f"{stem}*.srt")):
            return f"Dumps/{category}/{rel_dir}/{path.name}"
    return None


def video_link(category: str, rel_dir: str, media_file: Path) -> str:
    """The Area-note link for a saved video: the vault file while it lives
    there, or the watch page plus the in-vault transcript once it lives in
    the store."""
    if not enabled() or vault_dir(category, rel_dir) == media_file.parent:
        return f"[[Dumps/{category}/{rel_dir}/{media_file.name}]]"
    link = f"[▶ Watch]({watch_url(category, f'{rel_dir}/{media_file.name}')})"
    transcript = transcript_link(category, rel_dir, media_file.name)
    if transcript:
        link += f" · [[{transcript}|Transcript]]"
    return link


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def repo_rel(host: str, owner: str, repo: str) -> str:
    return f"{host}/{owner}/{repo}"


def repo_clone_dir(host: str, owner: str, repo: str) -> Path:
    """Where the full clone lives (or goes). A clone an older save left in
    the vault stays the clone until `python -m saver offload` moves it."""
    rel = repo_rel(host, owner, repo)
    in_vault = vault_dir("git", rel)
    if not enabled() or (in_vault / ".git" / "HEAD").exists():
        return in_vault
    return blob_dir("git", rel)


def mirror_repo(host: str, owner: str, repo: str) -> Path:
    """Copy what notes link to - the root README(s), or a whole gist - from
    the stored clone into the vault, plus the .offloaded marker (the URL of
    the clone's listing). Returns the vault folder."""
    rel = repo_rel(host, owner, repo)
    source, target = blob_dir("git", rel), vault_dir("git", rel)
    if not enabled() or source == target or not source.is_dir():
        return target
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        if host == GIST_HOST or path.name in README_NAMES:
            copy_into_vault(path, target / path.name)
    (target / MARKER).write_text(url("git", rel, directory=True) + "\n", encoding="utf-8")
    return target


# The extensions Self-hosted LiveSync stores as text (livesync-commonlib's
# isPlainText). It decodes them as UTF-8, so in any other encoding every
# non-ASCII character turns into U+FFFD on every device, and LiveSync then
# reports the file as corrupted (a GBK-encoded README, for instance).
LIVESYNC_TEXT_SUFFIXES = (".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas")


def as_utf8(data: bytes):
    """data re-encoded as UTF-8, or None when it already is (or can't be
    decoded with any confidence)."""
    try:
        data.decode("utf-8")
        return None
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes  # a dependency of requests

    best = from_bytes(data).best()
    if best is None:
        return None
    return str(best).encode("utf-8")


def copy_into_vault(source: Path, dest: Path):
    """Copy a repo file into the vault. Text files LiveSync syncs as text are
    converted to UTF-8 on the way; everything else is copied byte for byte."""
    converted = as_utf8(source.read_bytes()) if source.name.lower().endswith(LIVESYNC_TEXT_SUFFIXES) else None
    if converted is None:
        shutil.copy2(source, dest)
        return
    dest.write_bytes(converted)
    shutil.copystat(source, dest)
    log(f"Converted {source.name} to UTF-8 (LiveSync syncs it as text)")


def browse_link(host: str, owner: str, repo: str):
    if not enabled():
        return None
    rel = repo_rel(host, owner, repo)
    if not blob_dir("git", rel).is_dir():
        return None
    return f"[Browse files]({url('git', rel, directory=True)})"


def _repo_roots(base: Path):
    """Clone roots under base: host/owner/repo (gists: host/owner/id)."""
    if not base.is_dir():
        return
    for host in sorted(base.iterdir()):
        for owner in sorted(p for p in host.iterdir() if p.is_dir()) if host.is_dir() else []:
            for repo in sorted(p for p in owner.iterdir() if p.is_dir()):
                if (repo / ".git" / "HEAD").exists():
                    yield repo


def _fast_forward(root: Path):
    """Fast-forward a clone to its upstream. Returns an error message or None.

    Mode differences are ignored: a store on SMB/NFS often can't keep the
    executable bit, which makes every script look modified. Other edits to
    tracked files that block the update go to `git stash`, so they stay
    recoverable. Untracked files are left alone."""
    def git(*args, timeout=60):
        return _git("-C", str(root), "-c", "core.fileMode=false",
                    "-c", "user.name=obsidian-link-saver", "-c", "user.email=saver@localhost",  # for the stash commit
                    *args, timeout=timeout)

    def error(failed):
        return (failed.stderr or failed.stdout).strip()[-300:]

    fetched = git("fetch", "--quiet", timeout=300)
    if fetched.returncode != 0:
        return error(fetched)
    if git("merge-base", "--is-ancestor", "@{u}", "HEAD").returncode == 0:
        return None  # already up to date: local changes aren't in the way
    merged = git("merge", "--ff-only", "--quiet", "@{u}", timeout=300)
    if merged.returncode == 0:
        return None
    can_fast_forward = git("merge-base", "--is-ancestor", "HEAD", "@{u}").returncode == 0
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if not (can_fast_forward and dirty):
        return error(merged)
    stashed = git("stash", "push", "--quiet", "-m", "Local changes set aside by Update repos")
    if stashed.returncode != 0:
        return error(stashed)
    merged = git("merge", "--ff-only", "--quiet", "@{u}", timeout=300)
    if merged.returncode != 0:
        git("stash", "pop", "--quiet")
        return error(merged)
    log(f"{'/'.join(root.parts[-3:])}: local changes kept in git stash")
    return None


def update_repos():
    """Fast-forward every clone (store and any still in the vault) and
    refresh the in-vault README copies."""
    roots = list(_repo_roots(store_root() / "git")) if enabled() else []
    roots += list(_repo_roots(settings.git_dir))
    stage(f"Pulling {len(roots)} repositories")
    updated = unchanged = failed = 0
    for i, root in enumerate(roots, 1):
        check_cancelled()
        name = "/".join(root.parts[-3:])
        progress((i - 1) / max(len(roots), 1), f"{i}/{len(roots)} {name}")
        before = _git("-C", str(root), "rev-parse", "HEAD", timeout=30).stdout.strip()
        failure = _fast_forward(root)
        if failure is not None:
            failed += 1
            log(f"FAILED {name}: {failure}")
            continue
        after = _git("-C", str(root), "rev-parse", "HEAD", timeout=30).stdout.strip()
        if before != after:
            updated += 1
            log(f"updated {name}")
        else:
            unchanged += 1
        host, owner, repo = root.parts[-3:]
        if before != after and enabled() and store_root() / "git" in root.parents:
            record_write("git", f"{host}/{owner}/{repo}")
        mirror_repo(host, owner, repo)
    result(f"Repos: {updated} updated, {unchanged} unchanged, {failed} failed")


# ---------------------------------------------------------------------------
# usage: how much the store holds, for the dashboard
# ---------------------------------------------------------------------------
# Walking a store of hundreds of clones on a NAS array takes minutes, so the
# sizes live in the database (blob_usage), one row per "unit": the folder a
# save writes into as a whole - a repo (git/host/owner/repo), a YouTube
# channel (youtube/<channel>, holding its videos' folders), a conference
# (video/media.ccc.de/<conference>). This service is the store's only
# writer, so the store is walked in full once, and after that each write
# recounts just its unit (record_write). A full recount runs again after an
# offload, or from the dashboard's Recount button (for changes made outside
# the service, such as `python -m saver offload` run elsewhere).
UNIT_DEPTH = {"git": 3, "youtube": 1, "video": 2, "other": 1}
_count_lock = threading.Lock()
_counting = False
_count_error = None


def _tree_size(root: Path, count=None) -> tuple:
    """(bytes, matching files) under root; symlinks are neither followed nor counted."""
    total = matched = 0
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    if count and count(entry.name):
                        matched += 1
            except OSError:
                continue
    return total, matched


def _is_video_name(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS


def unit_of(category: str, rel: str) -> str:
    """The unit a path (relative to the category folder) belongs to."""
    return "/".join(Path(rel).parts[:UNIT_DEPTH[category]])


def _unit_path(category: str, unit: str) -> Path:
    return store_root() / unit if category == "other" else store_root() / category / unit


def _measure(category: str, path: Path) -> tuple:
    """(bytes, items) of one unit; bytes is None when it no longer exists."""
    if path.is_symlink():
        return 0, 0
    if path.is_file():
        return path.stat().st_size, int(category in ("youtube", "video") and _is_video_name(path.name))
    if not path.is_dir():
        return None, 0
    size, videos = _tree_size(path, _is_video_name)
    if category == "git":
        return size, int((path / ".git" / "HEAD").exists())
    return size, videos if category in ("youtube", "video") else 0


def _units(base: Path, depth: int, skip=(), prefix=()):
    """(unit, path) for everything under base, `depth` levels down; a file
    above that depth is a unit of its own."""
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if (not prefix and entry.name in skip) or entry.is_symlink():
            continue
        rel = prefix + (entry.name,)
        if entry.is_dir(follow_symlinks=False) and len(rel) < depth:
            yield from _units(Path(entry.path), depth, skip, rel)
        else:
            yield "/".join(rel), Path(entry.path)


def _full_count():
    global _counting, _count_error
    from .db import get_db
    db = get_db()
    started = time.time()
    try:
        for category in (*CATEGORIES, "other"):
            base, skip = (store_root(), CATEGORIES) if category == "other" else (store_root() / category, ())
            for unit, path in _units(base, UNIT_DEPTH[category], skip):
                size, items = _measure(category, path)
                db.set_blob_unit(category, unit, size, items, time.time())
        # units it didn't find are gone; ones a save recounted meanwhile are newer
        db.drop_blob_units_before(started)
        db.set_meta("blob_usage_counted_at", str(time.time()))
        _count_error = None
    except Exception as e:  # noqa: BLE001 - a stats walk must never take anything down
        _count_error = str(e)
    finally:
        with _count_lock:
            _counting = False


def recount() -> bool:
    """Walk the whole store in the background. False if a count is running."""
    global _counting
    if not enabled():
        return False
    with _count_lock:
        if _counting:
            return False
        _counting = True
    threading.Thread(target=_full_count, name="blob-usage", daemon=True).start()
    return True


def record_write(category: str, rel: str):
    """Recount the unit that a write to <store>/<category>/<rel> touched.
    Never fails the save that called it."""
    if not enabled():
        return
    try:
        from .db import get_db
        unit = unit_of(category, rel)
        size, items = _measure(category, _unit_path(category, unit))
        get_db().set_blob_unit(category, unit, size, items, time.time())
    except Exception as e:  # noqa: BLE001
        log(f"WARNING: could not update the blob store's size for {category}/{rel}: {e}")


def usage() -> dict:
    """The store's size per category, from the database. Starts the first
    full count when there has never been one; until it finishes, only
    "counting" is returned (partial sums would mislead)."""
    if not enabled():
        return None
    from .db import get_db
    db = get_db()
    counted_at = db.get_meta("blob_usage_counted_at")
    if counted_at is None and not _counting and _count_error is None:
        recount()
    status = {"counting": _counting}
    if counted_at is None:
        return {**status, "error": _count_error} if _count_error else status
    totals = db.blob_usage_totals()
    categories = {c: totals.get(c, {"bytes": 0, "items": 0}) for c in CATEGORIES}
    other = totals.get("other", {}).get("bytes", 0)
    return {**status, "counted_at": float(counted_at), "other_bytes": other,
            "bytes": sum(c["bytes"] for c in categories.values()) + other, "categories": categories}


# ---------------------------------------------------------------------------
# offload: move what older saves left in the vault into the store
# ---------------------------------------------------------------------------
def _walk_files(root: Path):
    """Relative paths of all files and symlinks under root (symlinks not followed)."""
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                dirnames.remove(d)
                filenames.append(d)
        for f in filenames:
            yield Path(os.path.relpath(os.path.join(dirpath, f), root))


def _repo_root(cat_dir: Path, rel: Path):
    for parent in rel.parents:
        if parent == Path("."):
            return None
        d = cat_dir / parent
        if (d / ".git").is_dir() or (d / MARKER).exists():
            return parent
    return None


def _notes():
    """Every Markdown note outside Dumps/ and dot-folders."""
    vault = settings.vault_dir
    for dirpath, dirnames, filenames in os.walk(vault):
        rel = Path(dirpath).relative_to(vault)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not (rel == Path(".") and d == "Dumps")]
        for f in filenames:
            if f.endswith(".md"):
                yield Path(dirpath) / f


# [[target]] / [[target|alias]] / ![[...]] - targets contain "[id]", so match
# lazily up to the first "|" or "]]" instead of excluding "]".
LINK_RE = re.compile(r"(!?)\[\[(Dumps/[^|\n]+?)(\|[^\]\n]*)?\]\]")


def _linked_targets() -> set:
    out = set()
    for note in _notes():
        for m in LINK_RE.finditer(note.read_text(encoding="utf-8", errors="replace")):
            target = m.group(2).split("#", 1)[0]
            if (settings.vault_dir / target).exists():
                out.add(target)
    return out


def _too_new(path: Path, now: float, temp_suffixes=TEMP_SUFFIXES) -> bool:
    return (path.name.endswith(temp_suffixes) or ".syncthing." in path.name
            or now - path.lstat().st_mtime < settings.offload_min_age)


def plan(category: str, linked: set) -> dict:
    """Videos: split a vault category into bulk (to move), keep (stays in
    the vault, also copied so the store folder is complete) and skipped
    (too new or in progress). Git is planned per repository instead, see
    plan_git()."""
    if category == "git":
        return plan_git(linked)
    cat_dir = settings.dumps_dir / category
    bulk, keep, skipped = [], [], []
    now = time.time()
    if not cat_dir.is_dir():
        return {"bulk": bulk, "keep": keep, "skipped": skipped, "bytes": 0}
    for rel in _walk_files(cat_dir):
        if rel.name.endswith(MARKER_SUFFIX):
            keep.append(rel)
        elif _too_new(cat_dir / rel, now):
            skipped.append(rel)
        else:
            (bulk if _is_media(rel) else keep).append(rel)
    size = sum((cat_dir / f).lstat().st_size for f in bulk)
    return {"bulk": bulk, "keep": keep, "skipped": skipped, "bytes": size}


def _is_repo_keep(rel: Path, root: Path, linked: set) -> bool:
    """What a repo leaves in the vault: its root README(s), whole gists,
    files a note links to, and the marker."""
    return (rel.name.endswith(MARKER_SUFFIX) or f"Dumps/git/{rel}" in linked
            or (rel.parts[0] == GIST_HOST and ".git" not in rel.relative_to(root).parts)
            or (rel.name in README_NAMES and rel.parent == root))


def plan_git(linked: set) -> dict:
    """Clones are re-cloned from their remote rather than copied (copying
    would need symlink support on the store: projects commit symlinks such
    as LICENSE -> COPYING). Planned per repository: one recently changed
    file or a missing .git/HEAD holds back the whole repo, never part of
    it. Files outside any repo are copied like videos."""
    cat_dir = settings.dumps_dir / "git"
    repos, loose_bulk, loose_keep, skipped = {}, [], [], []
    now = time.time()
    if cat_dir.is_dir():
        for rel in _walk_files(cat_dir):
            root = _repo_root(cat_dir, rel)
            if root is None or not (cat_dir / root / ".git").is_dir():
                # outside any repo, or what an earlier offload left behind
                if root is not None or rel.name.endswith(MARKER_SUFFIX) or f"Dumps/git/{rel}" in linked:
                    loose_keep.append(rel)
                elif _too_new(cat_dir / rel, now):
                    skipped.append(rel)
                else:
                    loose_bulk.append(rel)
                continue
            repo = repos.setdefault(root, {"bulk": [], "keep": [], "bytes": 0, "blocked": None})
            if not repo["blocked"] and not (cat_dir / root / ".git" / "HEAD").exists():
                repo["blocked"] = "incomplete clone (no .git/HEAD)"
            # No download-suffix rule here: repos legitimately contain files
            # like part_1.part (a CAD part) - only sync-tool temp files and
            # real recent changes mean "still being written".
            if not repo["blocked"] and _too_new(cat_dir / rel, now, temp_suffixes=()):
                repo["blocked"] = f"changed recently: {rel.relative_to(root)}"
            if _is_repo_keep(rel, root, linked):
                repo["keep"].append(rel)
            else:
                repo["bulk"].append(rel)
                repo["bytes"] += (cat_dir / rel).lstat().st_size
    size = sum(r["bytes"] for r in repos.values() if not r["blocked"])
    size += sum((cat_dir / f).lstat().st_size for f in loose_bulk)
    return {"repos": repos, "bulk": loose_bulk, "keep": loose_keep, "skipped": skipped, "bytes": size}


GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}  # a deleted/private repo must fail, not wait for a password


def _git(*args, timeout=60, **kwargs):
    # safe.directory: the store's clones may belong to another user (written
    # through SMB/NFS from the desktop, served by the container as nobody)
    return proc.run(["git", "-c", "safe.directory=*", *args], timeout=timeout,
                    env={**os.environ, **GIT_ENV}, **kwargs)


def remote_url(repo: Path):
    result = _git("-C", str(repo), "config", "--get", "remote.origin.url", timeout=30)
    url_ = result.stdout.strip()
    return url_ if result.returncode == 0 and url_ else None


def reclone(url_: str, dest: Path):
    """Shallow-clone url into dest via a temporary sibling, so dest is
    either a complete clone or absent. Returns (ok, error lines)."""
    tmp = dest.parent / f".{dest.name}.cloning"
    shutil.rmtree(tmp, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = _git("clone", "--depth", "1", "--quiet", url_, str(tmp), timeout=3600)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if result.returncode != 0 or not (tmp / ".git" / "HEAD").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        return False, (result.stderr or result.stdout).strip().splitlines()[-1:] or ["git clone failed"]
    if dest.exists():
        shutil.rmtree(dest)  # leftovers without a .git (README copies from an older run)
    tmp.rename(dest)
    return True, None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(src: Path, dest: Path, on_bytes) -> bool:
    """Copy src to dest and verify by SHA-256. Symlinks are copied as
    symlinks. An identical existing dest is accepted; a different one is a
    conflict (False) and left untouched."""
    if src.is_symlink():
        target = os.readlink(src)
        if dest.is_symlink() or dest.exists():
            return dest.is_symlink() and os.readlink(dest) == target
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, dest)
        return True
    size = src.stat().st_size
    if dest.exists():
        same = dest.stat().st_size == size and _sha256(dest) == _sha256(src)
        on_bytes(2 * size)
        return same
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".offload-tmp")
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        while chunk := fin.read(4 << 20):
            check_cancelled()
            digest.update(chunk)
            fout.write(chunk)
            on_bytes(len(chunk))
    shutil.copystat(src, tmp)
    if _sha256(tmp) != digest.hexdigest():
        tmp.unlink(missing_ok=True)
        raise OSError(f"verification failed for {dest}")
    on_bytes(size)
    os.replace(tmp, dest)
    return True


def _supports_symlinks(directory: Path) -> bool:
    probe = directory / ".symlink-probe"
    try:
        probe.unlink(missing_ok=True)
        os.symlink("target", probe)
        ok = probe.is_symlink() and os.readlink(probe) == "target"
        probe.unlink()
        return ok
    except OSError:
        return False


def _rewrite_links(moved: set) -> int:
    """Point note links at the watch page of every moved video."""
    changed = 0

    def repl(m):
        _bang, target, alias = m.groups()
        if target not in moved:
            return m.group(0)
        parts = Path(target).parts  # Dumps/<category>/.../<file>
        category, rel_file = parts[1], "/".join(parts[2:])
        label = alias[1:] if alias else "▶ Watch"
        link = f"[{label}]({watch_url(category, rel_file)})"
        transcript = transcript_link(category, str(Path(rel_file).parent), Path(rel_file).name)
        if transcript:
            link += f" · [[{transcript}|Transcript]]"
        return link

    for note in _notes():
        text = note.read_text(encoding="utf-8")
        new = LINK_RE.sub(repl, text)
        if new != text:
            changed += 1
            write_text_atomic(note, new)
            log(f"  links rewritten: {note.relative_to(settings.vault_dir)}")
    return changed


def _remove_empty_dirs(root: Path):
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p != root and not os.listdir(p):
            p.rmdir()


def _offload_git(git_plan: dict) -> dict:
    """Re-clone every movable repo into the store, then drop its internals
    from the vault (README and marker stay, refreshed from the new clone)."""
    cat_dir, store_git = settings.dumps_dir / "git", store_root() / "git"
    counts = {"recloned": 0, "in_store": 0, "left": 0, "freed": 0}
    repos = sorted(git_plan["repos"].items())
    stage(f"Re-cloning {sum(1 for _, r in repos if not r['blocked'])} repositories into the store")
    for i, (root, repo) in enumerate(repos, 1):
        check_cancelled()
        progress((i - 1) / max(len(repos), 1), f"repo {i}/{len(repos)}: {root}")
        if repo["blocked"]:
            log(f"left in the vault, {repo['blocked']}: Dumps/git/{root}")
            counts["left"] += 1
            continue
        if (store_git / root / ".git" / "HEAD").exists():
            counts["in_store"] += 1
        else:
            remote = remote_url(cat_dir / root)
            if not remote:
                log(f"left in the vault, no remote URL: Dumps/git/{root}")
                counts["left"] += 1
                continue
            ok, error = reclone(remote, store_git / root)
            if not ok:
                log(f"left in the vault, could not re-clone {remote}: {error[0]}")
                counts["left"] += 1
                continue
            counts["recloned"] += 1
        for rel in repo["bulk"]:
            path = cat_dir / rel
            if path.is_symlink() or path.is_file():
                counts["freed"] += path.lstat().st_size
                path.unlink()
        if len(root.parts) == 3:
            mirror_repo(*root.parts)  # fresh README copies + marker
        else:
            (cat_dir / root / MARKER).write_text(url("git", str(root), directory=True) + "\n", encoding="utf-8")
    _remove_empty_dirs(cat_dir)
    return counts


def _copy_category(cat: str, files: list, keep: list, on_bytes) -> tuple:
    """Copy files to the store, verified by SHA-256. Returns (moved, conflicts)."""
    cat_dir, dest_dir = settings.dumps_dir / cat, store_root() / cat
    moved, conflicts = [], 0
    for rel in files:
        check_cancelled()
        if _copy_verified(cat_dir / rel, dest_dir / rel, on_bytes):
            moved.append(rel)
        else:
            conflicts += 1
            log(f"CONFLICT, left in the vault: Dumps/{cat}/{rel} (a different file already exists in the store)")
    # Files that stay in the vault are copied too, so the store holds
    # complete video folders; a differing copy there is fine.
    for rel in keep:
        if not rel.name.endswith(MARKER_SUFFIX):
            _copy_verified(cat_dir / rel, dest_dir / rel, lambda n: None)
    return moved, conflicts


def offload(apply: bool = False, categories=CATEGORIES):
    """Move the bulky files that saves made without a store left in the vault into it:
    video files are copied and verified by SHA-256, git clones re-cloned
    from their remotes. Nothing leaves the vault unless its store copy is
    in place. Links in notes to moved videos are rewritten to the watch
    page, markers are written; the sync tool then frees the space on the
    other devices."""
    if not enabled():
        raise RuntimeError("blob store not configured (set BLOB_DIR and PUBLIC_URL)")
    stage("Planning")
    linked = _linked_targets()
    plans = {cat: plan(cat, linked) for cat in categories}
    video_bytes = sum(plans[c]["bytes"] for c in categories if c != "git")
    for cat, p in plans.items():
        if cat != "git":
            log(f"{cat}: move {len(p['bulk'])} files ({human(p['bytes'])}), keep {len(p['keep'])}, "
                f"skip {len(p['skipped'])} (changed recently or in progress)")
    git = plans.get("git")
    if git:
        movable = [r for r in git["repos"].values() if not r["blocked"]]
        log(f"git: {len(git['repos'])} clones - {len(movable)} to re-clone or already in the store "
            f"({human(sum(r['bytes'] for r in movable))} to free), {len(git['repos']) - len(movable)} held back "
            f"(changed recently or incomplete); {len(git['bulk'])} loose files to copy")
    total = video_bytes + (git["bytes"] if git else 0)
    if not apply:
        if git:
            store_git = store_root() / "git"
            for root, r in sorted(git["repos"].items()):
                if r["blocked"]:
                    log(f"  held back ({r['blocked']}): Dumps/git/{root}")
                elif not (store_git / root / ".git" / "HEAD").exists() \
                        and not remote_url(settings.dumps_dir / "git" / root):
                    log(f"  no remote URL, would stay in the vault: Dumps/git/{root}")
        videos = sum(len(plans[c]["bulk"]) for c in categories if c != "git")
        clones = sum(1 for r in git["repos"].values() if not r["blocked"]) if git else 0
        result(f"Dry run: {human(total)} would move out of the vault ({videos} video files"
               + (f", {clones} git clones re-cloned" if git else "") + ")")
        return plans

    links = sum(1 for cat, p in plans.items() for rel in p["bulk"]
                if (settings.dumps_dir / cat / rel).is_symlink())
    if links and not _supports_symlinks(store_root()):
        raise RuntimeError(f"the blob store at {store_root()} can't hold symlinks, and {links} files to copy "
                           "are symlinks - nothing was moved")

    done_bytes = [0]
    copy_total = max(sum(p["bytes"] for c, p in plans.items() if c != "git") * 2, 1)

    def on_bytes(n):
        done_bytes[0] += n
        progress(min(done_bytes[0] / copy_total, 1.0), f"{human(done_bytes[0] / 2)} copied and verified")

    freed = conflicts = 0
    summary = []
    for cat, p in plans.items():
        cat_dir = settings.dumps_dir / cat
        if cat == "git":
            counts = _offload_git(p)
            freed += counts["freed"]
            summary.append(f"{counts['recloned']} repos re-cloned, {counts['in_store']} already in the store, "
                           f"{counts['left']} left in the vault")
            if p["bulk"]:
                moved, c = _copy_category(cat, p["bulk"], [], on_bytes)
                conflicts += c
                for rel in moved:
                    freed += (cat_dir / rel).lstat().st_size
                    (cat_dir / rel).unlink()
                _remove_empty_dirs(cat_dir)
            continue
        if not p["bulk"]:
            continue
        stage(f"Copying {cat} to the store")
        moved, c = _copy_category(cat, p["bulk"], p["keep"], on_bytes)
        conflicts += c
        stage(f"Updating notes for {cat}")
        with vault_lock():
            for rel in moved:
                if _is_media(rel):
                    folder = cat_dir / rel.parent
                    videos = [f for f in folder.iterdir() if f.is_file() and _is_media(f)]
                    write_video_markers(folder, cat, str(rel), only_video_in_folder=len(videos) == 1)
            _rewrite_links({f"Dumps/{cat}/{rel}" for rel in moved if _is_media(rel)})
            for rel in moved:
                path = cat_dir / rel
                if path.is_symlink() or path.is_file():
                    freed += path.lstat().st_size
                    path.unlink()
        _remove_empty_dirs(cat_dir)
        summary.append(f"{len(moved)} {cat} files moved")
    recount()  # an offload moves many units at once
    result(f"Offloaded {human(freed)} from the vault ({'; '.join(summary) or 'nothing to move'})"
           + (f", {conflicts} conflict(s) left in place" if conflicts else ""))
    return plans
