"""yt-dlp metadata and downloads, subtitle muxing, and the transcription
hook. Downloads stream yt-dlp's progress into the job, and cookies come
from a cookies.txt file."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import proc
from .config import settings
from .urls import extract_youtube_id
from .jobctx import JobCancelled, check_cancelled, log, progress, stage

MEDIA_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
_PROGRESS_RE = re.compile(r"^\[progress\]\s*([\d.]+)%\|([^|]*)\|([^|]*)$")


def ytdlp_base(use_cookies: bool = True) -> list:
    """use_cookies=False for hosts that don't need a logged-in session -
    media.ccc.de serves everything anonymously."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found on PATH")
    cmd = ["yt-dlp"]
    cookies = settings.cookies_path
    if use_cookies and cookies:
        cmd += ["--cookies", str(cookies)]
    return cmd + list(settings.ytdlp_extra_args)


def ytdlp_metadata(url: str, use_cookies: bool = True) -> dict:
    """yt-dlp's raw --dump-json for a single video.

    --no-playlist because a video URL copied out of the browser usually
    carries the playlist it was watched in (...&list=...&index=4). Without
    it yt-dlp resolves every video in that playlist (41s instead of 4s for a
    10-video list) and --dump-json emits one JSON object per video, which
    json.loads() can't read anyway."""
    cmd = ytdlp_base(use_cookies) + ["--no-playlist", "--dump-json", "--skip-download", "--no-warnings", url]
    result = proc.run(cmd, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-1000:] or "yt-dlp exited non-zero")
    return json.loads(result.stdout)


def fetch_youtube_metadata(url: str):
    data = ytdlp_metadata(url)
    return {
        "id": data.get("id"),
        "title": data.get("title") or "Untitled",
        "uploader": data.get("uploader") or "",
        "description": data.get("description") or "",
    }


def fetch_ccc_metadata(url: str):
    """media.ccc.de fields differ from YouTube's: there is no uploader or
    channel, the speaker is in `creator`, and the conference is only
    recoverable as the slug prefix of display_id (39c3-css-clicker...)."""
    data = ytdlp_metadata(url, use_cookies=False)
    display_id = data.get("display_id") or ""
    conference = display_id.split("-", 1)[0] if "-" in display_id else ""
    creators = data.get("creators") or ([data["creator"]] if data.get("creator") else [])
    return {
        "id": str(data.get("id") or ""),
        "title": data.get("title") or "Untitled",
        "speakers": ", ".join(creators),
        "conference": conference,
        "description": data.get("description") or "",
        "duration": data.get("duration") or 0,
        "filesize": data.get("filesize") or data.get("filesize_approx") or 0,
    }


def fetch_youtube_channel_metadata(url: str):
    """Metadata for a whole channel/playlist via flat-playlist mode (lists
    entries without resolving each video)."""
    cmd = ytdlp_base() + ["--flat-playlist", "--dump-single-json", "--no-warnings", url]
    result = proc.run(cmd, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-1000:] or "yt-dlp exited non-zero")
    data = json.loads(result.stdout)
    entries = [{"id": e.get("id"), "title": e.get("title") or e.get("id")}
               for e in (data.get("entries") or []) if e.get("id")]
    return {
        "title": data.get("title") or data.get("uploader") or "Untitled channel",
        "uploader": data.get("uploader") or data.get("channel") or "",
        "description": data.get("description") or "",
        "entries": entries,
    }


PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


def remove_partial_downloads(target_dir: Path):
    """After a cancelled download: drop yt-dlp's partial files (and the
    folder, if nothing else is in it) so they don't sit in the vault."""
    if not target_dir.is_dir():
        return
    for path in target_dir.iterdir():
        if path.is_file() and (path.name.endswith(PARTIAL_SUFFIXES) or ".part-Frag" in path.name):
            path.unlink(missing_ok=True)
    try:
        target_dir.rmdir()
    except OSError:
        pass  # not empty: keep what was already there


def run_ytdlp_download(args: list, cwd: Path, use_cookies: bool = True, what: str = "video"):
    """Run a yt-dlp download, streaming its output: progress lines drive the
    job's progress bar, everything else goes to the job log. Separate
    video and audio streams are downloaded one after the other, so the
    percentage restarts once - shown as "stream 2"."""
    cmd = ytdlp_base(use_cookies) + [
        "--newline",
        "--progress-template",
        "download:[progress] %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        *args,
    ]
    process = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    tail, stream, last_pct = [], 1, 0.0
    try:
        for raw in process.stdout:
            # Checked on every line: during a download yt-dlp prints little
            # besides progress lines, so checking only on other output meant
            # Cancel was ignored until the video finished.
            check_cancelled()
            line = raw.rstrip()
            match = _PROGRESS_RE.match(line.strip())
            if match:
                pct = float(match.group(1))
                if pct + 5 < last_pct:
                    stream += 1
                last_pct = pct
                speed, eta = match.group(2).strip(), match.group(3).strip()
                label = f"{what}{f' (stream {stream})' if stream > 1 else ''}: {pct:.0f}%"
                if speed and "Unknown" not in speed:
                    label += f" · {speed}"
                if eta and "Unknown" not in eta:
                    label += f" · ETA {eta}"
                progress(pct / 100.0, label)
                continue
            if line:
                log(line)
                tail = (tail + [line])[-20:]
        process.wait()
    except BaseException as e:
        process.kill()
        process.wait()
        if isinstance(e, JobCancelled):
            remove_partial_downloads(Path(cwd))
        raise
    if process.returncode != 0:
        raise RuntimeError("yt-dlp failed: " + " | ".join(tail[-5:]))


def _new_media_file(target_dir: Path, before: set, media_id: str = None) -> Path:
    """The file yt-dlp just produced. When it produced nothing because the
    video was already downloaded (a force re-save, e.g. to add a missing
    transcript), reuse the existing file instead of treating the download
    as failed, which would drop the transcript and the Area link. Files
    are named "<title> [<id>].<ext>", so `media_id` picks the right one in
    a shared channel folder."""
    new_files = [target_dir / n for n in ({p.name for p in target_dir.iterdir()} - before)]
    media = [p for p in new_files if p.suffix.lower() in MEDIA_EXTS] or new_files
    if not media:
        media = [p for p in target_dir.iterdir()
                 if p.suffix.lower() in MEDIA_EXTS and ".subs-tmp" not in p.name
                 and (media_id is None or f"[{media_id}]" in p.name)]
        if media:
            log(f"Already downloaded, reusing {max(media, key=lambda p: p.stat().st_mtime).name}")
    if not media:
        raise RuntimeError("yt-dlp reported success but produced no file")
    return max(media, key=lambda p: p.stat().st_mtime)


def download_youtube_video(url: str, target_dir: Path) -> Path:
    """Download the video and transcribe it, both landing in target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in target_dir.iterdir()}
    stage("Downloading video")
    run_ytdlp_download([
        # Same reason as ytdlp_metadata, and worse here: a ...&list=... URL
        # downloads the entire playlist, and the "newest new file" pick below
        # then transcribes whichever video finished last.
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mkv",
        # ".*-orig" is yt-dlp's tag for the one auto-caption track in the
        # video's actual spoken language (every other code is a machine
        # translation of it).
        "--write-auto-subs", "--sub-langs", ".*-orig", "--sub-format", "srt", "--embed-subs",
        "-o", "%(title)s [%(id)s].%(ext)s",
        url,
    ], target_dir)
    video_file = _new_media_file(target_dir, before, extract_youtube_id(url))
    return transcribe_and_embed(video_file, target_dir)


def download_ccc_talk(url: str, target_dir: Path) -> Path:
    """One media.ccc.de talk, then transcribed like a YouTube save. No
    cookies (served anonymously). --write-subs (real uploaded tracks) rather
    than YouTube's auto-caption scheme; talks often ship none, which is why
    the Whisper pass still runs."""
    target_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in target_dir.iterdir()}
    stage("Downloading talk")
    run_ytdlp_download([
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mkv",
        "--write-subs", "--sub-langs", "all", "--sub-format", "srt", "--embed-subs",
        "-o", "%(title)s [%(id)s].%(ext)s",
        url,
    ], target_dir, use_cookies=False, what="talk")
    video_file = _new_media_file(target_dir, before)
    return transcribe_and_embed(video_file, target_dir)


def download_youtube_channel_video(video_id: str, target_dir: Path) -> Path:
    """One video of a channel save, by id. Deliberately skips subtitles and
    Whisper: running Whisper across a whole channel would take forever, and
    bulk channel saves just want the videos."""
    before = {p.name for p in target_dir.iterdir()}
    run_ytdlp_download([
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mkv",
        "--no-write-auto-subs", "--no-write-subs",
        "-o", "%(title)s [%(id)s].%(ext)s",
        f"https://www.youtube.com/watch?v={video_id}",
    ], target_dir)
    return _new_media_file(target_dir, before, video_id)


def embed_subtitles(video_file: Path, srt_path: Path) -> Path:
    """Mux the Whisper .srt into video_file as its first subtitle track.
    Returns the resulting path, which may differ from the input.

    ffmpeg can't edit a container in place, so this muxes into a sibling
    temp file and renames it over the original - on failure the original is
    left as-is. The output is always Matroska (the only container here that
    takes SubRip by stream copy); a non-.mkv input is renamed to .mkv rather
    than keeping a lying extension (media.ccc.de serves a pre-muxed mp4)."""
    if not shutil.which("ffmpeg"):
        log("WARNING: ffmpeg not on PATH, skipping subtitle embedding")
        return video_file
    stage("Embedding subtitles")
    tmp_path = video_file.with_name(video_file.stem + ".subs-tmp.mkv")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_file),
        "-i", str(srt_path),
        # drop existing subtitle streams from the first -map, insert the
        # Whisper srt, then re-append the existing ones, so Whisper always
        # lands as the first subtitle track.
        "-map", "0", "-map", "-0:s",
        "-map", "1",
        "-map", "0:s?",
        "-c", "copy", "-c:s", "srt",
        "-metadata:s:s:0", "title=Whisper (auto-generated)",
        str(tmp_path),
    ]
    result = proc.run(cmd)
    if result.returncode != 0 or not tmp_path.exists():
        log(f"WARNING: failed to embed subtitles into {video_file.name}:\n{result.stderr[-2000:]}")
        tmp_path.unlink(missing_ok=True)
        return video_file
    final = video_file if video_file.suffix.lower() == ".mkv" else video_file.with_suffix(".mkv")
    tmp_path.replace(final)
    if final != video_file:
        video_file.unlink(missing_ok=True)
    log(f"Embedded subtitles into {final.name}")
    return final


def transcribe_and_embed(video_file: Path, target_dir: Path) -> Path:
    """Whisper the video and mux the result back in. Failures here are
    warnings, never fatal - a video without a transcript is still worth
    keeping."""
    if not settings.whisper_enabled:
        log("Whisper disabled (WHISPER_ENABLED=false), skipping transcript")
        return video_file
    stage("Transcribing")
    try:
        from . import transcribe
        srt_path = transcribe.transcribe(video_file, target_dir)
    except ImportError:
        log("WARNING: faster-whisper is not installed, skipping transcript")
        return video_file
    except JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001 - keep the download even if Whisper breaks
        log(f"WARNING: transcription failed for {video_file.name}: {e}")
        return video_file
    return embed_subtitles(video_file, srt_path)
