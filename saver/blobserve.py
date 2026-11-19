"""HTTP access to the blob store: /blobs (files and folder listings),
/watch (video player with subtitles and ?t= start time) and /subs
(subtitles as WebVTT, which is all <track> accepts).

These routes are deliberately not behind API_TOKEN - note links open them
straight from Obsidian or the browser, like the Caddy share they replace.
Keep the port on the LAN or behind your reverse proxy's auth.

Stored files can come from arbitrary git repositories, so they're served
with `Content-Security-Policy: sandbox` (an HTML or SVG file from a repo
gets an opaque origin and can't script against the API on this origin),
paths are confined to the store after resolving symlinks, and nothing is
ever executed.
"""
import html
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .blobs import VIDEO_EXTS, human
from .config import settings

router = APIRouter(include_in_schema=False)

MEDIA_TYPES = {
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".opus": "audio/ogg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".vtt": "text/vtt; charset=utf-8", ".srt": "text/plain; charset=utf-8",
}
BINARY_PREFIXES = ("image/", "audio/", "video/", "font/")
BINARY_TYPES = {"application/pdf", "application/zip", "application/gzip", "application/x-tar",
                "application/x-7z-compressed", "application/octet-stream", "application/wasm"}
FILE_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
README_PREVIEW_BYTES = 200_000


def _root() -> Path:
    if not settings.blob_dir:
        raise HTTPException(status_code=404, detail="no blob store configured")
    return Path(settings.blob_dir).resolve()


def _resolve(path: str) -> Path:
    root = _root()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404)
    if not target.exists():
        raise HTTPException(status_code=404)
    return target


def _looks_like_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start >= len(chunk) - 3  # a multibyte char cut off by the read
    return True


def content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MEDIA_TYPES:
        return MEDIA_TYPES[ext]
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed and (guessed.startswith(BINARY_PREFIXES) or guessed in BINARY_TYPES):
        return guessed
    # source files, READMEs, configs - whatever mimetypes thinks .rs or .ts is
    if _looks_like_text(path):
        return "text/plain; charset=utf-8"
    return guessed or "application/octet-stream"


def _href(rel: str, prefix: str = "/blobs") -> str:
    return f"{prefix}/{quote(rel, safe='/')}"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/blobs.css"></head>
<body><header class="topbar"><a class="brand" href="/"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l10 6v14l-10 6-10-6V9z"/><path class="arrow" d="M16 9v14M11 18l5 5 5-5"/></svg><span>Obsidian Saver</span></a>
<nav class="crumbs">{crumbs}</nav></header><main class="blob-main">{body}</main></body></html>"""


def _crumbs(rel: str) -> str:
    parts = [p for p in rel.split("/") if p]
    out = ['<a href="/blobs/">store</a>']
    for i, part in enumerate(parts):
        link = _href("/".join(parts[: i + 1])) + ("/" if i < len(parts) - 1 else "")
        out.append(f'<a href="{link}">{html.escape(part)}</a>')
    return '<span class="sep">/</span>'.join(out)


def _listing(directory: Path, rel: str) -> str:
    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    rows, readme = [], None
    for entry in entries:
        if entry.name.startswith("."):
            continue
        child = f"{rel.rstrip('/')}/{entry.name}".lstrip("/")
        try:
            stat = entry.stat()
        except OSError:
            continue
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        if entry.is_dir():
            rows.append(f'<tr><td><a class="dir" href="{_href(child)}/">{html.escape(entry.name)}/</a></td>'
                        f'<td></td><td class="muted">{modified}</td><td></td></tr>')
            continue
        watch = (f'<a class="watch" href="{_href(child, "/watch")}">▶ Watch</a>'
                 if entry.suffix.lower() in VIDEO_EXTS else "")
        rows.append(f'<tr><td><a href="{_href(child)}">{html.escape(entry.name)}</a></td>'
                    f'<td class="num">{human(stat.st_size)}</td><td class="muted">{modified}</td><td>{watch}</td></tr>')
        if readme is None and entry.name.lower().startswith("readme") and stat.st_size <= README_PREVIEW_BYTES:
            readme = entry
    body = [f'<section class="card"><h1>{html.escape(directory.name or "store")}</h1>',
            '<table class="listing"><thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead><tbody>',
            "".join(rows) or '<tr><td class="muted" colspan="4">Empty folder</td></tr>', "</tbody></table></section>"]
    if readme is not None:
        text = readme.read_text(encoding="utf-8", errors="replace")
        body.append(f'<section class="card"><h2>{html.escape(readme.name)}</h2><pre class="readme">{html.escape(text)}</pre></section>')
    return PAGE.format(title=html.escape(rel or "Blob store"), crumbs=_crumbs(rel), body="".join(body))


def srt_to_vtt(text: str) -> str:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    text = re.sub(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text


def _subtitle_tracks(media: Path, rel: str):
    folder_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    stem = media.stem
    tracks, seen = [], set()
    candidates = sorted(media.parent.iterdir(), key=lambda p: (not p.name.startswith("out."), p.name))
    for path in candidates:
        if path.suffix.lower() not in (".vtt", ".srt") or not path.is_file():
            continue
        if not (path.name.startswith("out.") or path.name.startswith(stem)):
            continue
        key = path.stem
        if key in seen:
            continue  # out.vtt and out.srt are the same transcript
        seen.add(key)
        if path.name.startswith("out."):
            label = "Whisper"
        else:
            lang = path.stem[len(stem):].strip(".") or "subtitles"
            label = f"YouTube ({lang})"
        tracks.append((label, _href(f"{folder_rel}/{path.name}".lstrip("/"), "/subs")))
    return tracks


YOUTUBE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


def original_url(rel: str):
    """Where a stored video came from, for the watch page's "Original" link.
    A YouTube id in the file or folder name ([xxxxxxxxxxx]) wins: channel
    downloads share one folder, so the folder alone can't tell them apart.
    Otherwise the save registry's entry for the video's own folder (single
    YouTube saves, media.ccc.de talks)."""
    from .db import get_db

    db = get_db()
    match = YOUTUBE_ID_RE.search(Path(rel).name) or YOUTUBE_ID_RE.search(rel)
    if match:
        entry = db.get_entry(f"url:youtube.com/watch?v={match.group(1)}")
        return entry["url"] if entry else f"https://www.youtube.com/watch?v={match.group(1)}"
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    entry = db.entry_by_note(f"Dumps/{folder}") if folder else None
    return entry["url"] if entry else None


def _watch_page(media: Path, rel: str) -> str:
    title = re.sub(r"\s*\[[^\]]*\]$", "", media.stem)
    folder_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    tracks = _subtitle_tracks(media, rel)
    track_tags = "".join(
        f'<track kind="subtitles" label="{html.escape(label)}" src="{src}"{" default" if i == 0 else ""}>'
        for i, (label, src) in enumerate(tracks))
    transcript = media.parent / "out.txt"
    links = [f'<a href="{_href(rel)}" download>Download ({human(media.stat().st_size)})</a>',
             f'<a href="{_href(folder_rel)}/">Folder</a>']
    if transcript.is_file():
        links.append(f'<a href="{_href(f"{folder_rel}/out.txt".lstrip("/"))}">Transcript</a>')
    source = original_url(rel)
    if source:
        host = re.sub(r"^www\.", "", source.split("/")[2]) if "://" in source else "source"
        links.append(f'<a id="original" href="{html.escape(source)}" target="_blank" rel="noopener noreferrer">'
                     f'Original ({html.escape(host)})</a>')
    body = f"""<section class="card watch-card">
<h1>{html.escape(title)}</h1>
<video id="player" controls autoplay playsinline preload="metadata" src="{_href(rel)}">{track_tags}</video>
<p class="watch-links">{' · '.join(links)}</p>
<p class="muted small">Start at a time with <code>?t=754</code> or <code>?t=12:34</code>. If the video doesn't play, your browser can't decode this container/codec - use Download.</p>
</section>
<script>
(() => {{
  // "Original" continues where the player is: YouTube takes ?t=<n>s,
  // media.ccc.de #t=<n>.
  const player = document.getElementById("player");
  const original = document.getElementById("original");
  if (original) {{
    const base = original.href;
    const update = () => {{
      const t = Math.floor(player.currentTime || 0);
      if (t < 1) {{ original.href = base; return; }}
      const url = new URL(base);
      if (/(^|\\.)youtube\\.com$|^youtu\\.be$/.test(url.hostname)) url.searchParams.set("t", t + "s");
      else url.hash = "t=" + t;
      original.href = url.toString();
    }};
    ["mouseenter", "focus", "mousedown", "touchstart"].forEach((e) => original.addEventListener(e, update));
  }}
}})();
(() => {{
  const params = new URLSearchParams(location.search);
  const raw = params.get("t") || (location.hash.match(/t=([\\d:.]+)/) || [])[1];
  if (!raw) return;
  const seconds = raw.split(":").reduce((acc, part) => acc * 60 + parseFloat(part || 0), 0);
  const player = document.getElementById("player");
  const seek = () => {{ player.currentTime = seconds; }};
  if (player.readyState >= 1) seek(); else player.addEventListener("loadedmetadata", seek, {{ once: true }});
}})();
</script>"""
    return PAGE.format(title=html.escape(title), crumbs=_crumbs(rel), body=body)


@router.get("/blobs", include_in_schema=False)
def blobs_root():
    return RedirectResponse("/blobs/")


@router.get("/blobs/{path:path}")
def blob(path: str, request: Request):
    target = _resolve(path)
    if target.is_dir():
        if path and not request.url.path.endswith("/"):
            return RedirectResponse(request.url.path + "/")
        return HTMLResponse(_listing(target, path.rstrip("/")))
    return FileResponse(target, media_type=content_type(target), headers=FILE_HEADERS)


@router.get("/watch/{path:path}")
def watch(path: str):
    target = _resolve(path)
    if not target.is_file() or target.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="not a video")
    return HTMLResponse(_watch_page(target, path))


@router.get("/subs/{path:path}")
def subtitles(path: str):
    target = _resolve(path)
    if not target.is_file() or target.suffix.lower() not in (".srt", ".vtt"):
        raise HTTPException(status_code=404)
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix.lower() == ".srt":
        text = srt_to_vtt(text)
    return Response(text, media_type="text/vtt; charset=utf-8", headers=FILE_HEADERS)
