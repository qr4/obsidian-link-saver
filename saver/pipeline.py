"""The save pipeline: turn a URL (or an uploaded file) into a dump under
Dumps/ plus a tagged, summarized bullet in an Areas/ leaf note.

  - dedupe state lives in SQLite (db.claim_entry/complete_entry)
  - progress goes to the job (stage()/progress()/log())
  - errors raise SaveError, which fails the job, not the service
"""
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import blobs, gitrepos, llm, media
from .config import settings
from .db import get_db
from .errors import ContentFreeError, SaveError
from .fetch import (
    PDF_TEXT_MIN_CHARS,
    extract_pdf_metadata_title,
    extract_pdf_text,
    extract_singlefile_source_url,
    fetch_page_html,
    fetch_pdf,
    find_identical_pdf,
    looks_like_pdf,
    pdf_import_source_url,
    pdf_metadata_digest,
    pdf_page_count,
    save_single_file,
)
from .jobctx import JobCancelled, check_cancelled, current, log, progress, result, span, stage
from .urls import (
    GIST_HOST,
    extract_youtube_id,
    is_ccc_talk_url,
    is_git_author_url,
    is_pdf_url,
    is_youtube_channel_url,
    is_youtube_url,
    normalize_url,
    parse_git_repo_url,
)
from .util import derive_url_slug_title, move_file, safe_segment, sanitize_filename, unique_destination
from .vaultnotes import append_area_entry, materialize_area, replace_area_entry, validate_area_path


def _job_id():
    reporter = current()
    return reporter.job_id if reporter else None


def _park_duplicate(path: Path):
    """Move an already-saved inbox file aside so the watcher doesn't
    re-flag it. Uploads are just deleted - nobody looks inside appdata."""
    if Path(settings.upload_dir).resolve() in path.resolve().parents:
        path.unlink(missing_ok=True)
        return
    dup_dir = path.parent / "_duplicate"
    dup_dir.mkdir(exist_ok=True)
    path.rename(unique_destination(dup_dir, path.name))


# The start of a video's transcript is what the LLM files it by: about the
# first few minutes of speech, enough to tell the subject, short enough to
# leave room for the prompt around it.
TRANSCRIPT_CHARS = 4000


def video_text(header: str, description: str, folder) -> str:
    """Header (title, channel...), the start of the Whisper transcript when
    the download produced one, then the description. A description alone
    misleads: it's often promotion, a link list, or about something else
    entirely ("Me at the zoo" now links microplastics research)."""
    parts = [header]
    transcript = Path(folder) / "out.txt" if folder else None
    if transcript is not None and transcript.is_file():
        spoken = transcript.read_text(encoding="utf-8", errors="replace").strip()
        if spoken:
            parts.append("Transcript (start):\n" + spoken[:TRANSCRIPT_CHARS])
    if description:
        parts.append("Description:\n" + description)
    return "\n\n".join(parts)


class Saver:
    # ------------------------------------------------------------------
    # shared steps
    # ------------------------------------------------------------------
    def analyze(self, text, allow_thin=False, require_content=True):
        """Tags, summary and Area leaf for a piece of text - the LLM half
        every content type shares."""
        stage("Tagging")
        tags = llm.extract_tags(text)
        check_cancelled()
        stage("Summarizing")
        summary = llm.extract_summary(text, require_content=require_content and not allow_thin)
        check_cancelled()
        stage("Choosing an Area")
        area = self.resolve_topic_area(tags, summary)
        log("TAGS:", tags)
        log("SUMMARY:", summary)
        log("AREA:", area or "(uncategorized)")
        return tags, summary, area

    def resolve_topic_area(self, tags, summary) -> str:
        area = validate_area_path(llm.extract_area(tags, summary))
        materialize_area(area)
        return area

    def write_entry(self, area, tags, summary, source_line, prior_area_note=None, prior_area_entry=None):
        stage("Writing Area note")
        written = replace_area_entry(area, tags, summary, source_line, prior_area_note, prior_area_entry)
        self.refresh_indexes()
        return written

    def analyze_download(self, text, allow_thin, folder: Path, created: bool):
        """analyze() for a video that is already downloaded. When the LLM
        rejects it as content-free, a folder this save created is removed:
        nothing would ever link to it, and it can hold gigabytes."""
        try:
            return self.analyze(text, allow_thin=allow_thin)
        except ContentFreeError:
            if created and folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
                log(f"Removed the download, nothing links to it: {folder}")
            raise

    @staticmethod
    def refresh_indexes():
        """List a new leaf in its folder notes right away, in the same sync
        batch as the leaf itself (the waypoint keeper would catch it too)."""
        if settings.manage_waypoints:
            from .waypoints import keeper
            keeper.refresh_now()

    # ------------------------------------------------------------------
    # generic web page
    # ------------------------------------------------------------------
    def save_generic(self, url, precaptured=None, existing_filename=None,
                     existing_area_note=None, existing_area_entry=None, allow_thin=False):
        now = datetime.now()
        page_dir = Path(settings.page_dump_dir)

        if precaptured:
            html, temp_path = precaptured
            log("Using SingleFile capture from the browser inbox")
        else:
            stage("Capturing page")
            temp_name = f"_capture-{now.strftime('%Y%m%d%H%M%S%f')}.html"
            temp_path = None
            try:
                temp_path = save_single_file(url, page_dir, temp_name)
                html = temp_path.read_text(encoding="utf-8", errors="ignore")
                log("Captured full snapshot via SingleFile")
            except Exception as e:  # noqa: BLE001
                log(f"WARNING: SingleFile capture failed ({e}), falling back to a plain HTTP fetch")
                html = fetch_page_html(url)

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        # Until the capture has its final name, any failure (content rejected,
        # the LLM timing out, a cancel) removes the temp capture, so a dropped
        # save leaves nothing in the vault.
        try:
            tags, summary, area = self.analyze(text, allow_thin=allow_thin)

            # <title> is often useless as a distinctive filename - some sites
            # reuse one static <title> site-wide - so ask the model for the
            # real title, falling back to the DOM title plus a URL slug.
            stage("Naming the dump")
            dom_title = soup.title.string.strip() if soup.title and soup.title.string else ""
            title = llm.extract_title(text) or dom_title or url
            if dom_title and title.strip().lower() == dom_title.strip().lower():
                slug_title = derive_url_slug_title(url)
                if slug_title and slug_title.lower() != title.strip().lower():
                    title = f"{title} - {slug_title}"

            page_dir.mkdir(parents=True, exist_ok=True)
            if existing_filename and (page_dir / existing_filename).exists():
                # force redo: overwrite the same file in place rather than
                # minting a fresh timestamped name and orphaning the old capture.
                dest = page_dir / existing_filename
            else:
                timestamp = now.strftime("%Y-%m-%d %H-%M-%S")
                final_name = f"{sanitize_filename(title)[:150]} ({timestamp}).html"
                dest = unique_destination(page_dir, final_name)
            if temp_path:
                move_file(temp_path, dest)
            else:
                dest.write_text(html, encoding="utf-8")
        except BaseException:
            if temp_path and not precaptured:
                temp_path.unlink(missing_ok=True)
            raise
        filename = dest.name

        source_line = f"[[Dumps/single_page/{filename}]]\n{url}"
        area_note, area_entry = self.write_entry(area, tags, summary, source_line,
                                                 existing_area_note, existing_area_entry)
        return "Areas", area, filename, area_note, area_entry

    # ------------------------------------------------------------------
    # git
    # ------------------------------------------------------------------
    def save_git_repo(self, url, host=None, owner=None, repo=None,
                      existing_area_note=None, existing_area_entry=None):
        """host/owner/repo can be pre-supplied by detect_git_repo, which has
        already verified a host outside the GIT_HOSTS allowlist - re-deriving
        them here would reject that host."""
        if host is None:
            host, raw_owner, raw_repo = parse_git_repo_url(url)
            owner = safe_segment(raw_owner) if raw_owner else "unknown"
            repo = safe_segment(raw_repo) if raw_repo else None
        repo_label = f"{host}/{owner}/{repo}" if repo else f"{host}/{owner}"

        # A bare '.git' URL isn't a browsable page - strip it for tagging.
        browse_url = url[:-4] if url.endswith(".git") else url
        stage("Reading repo page")
        try:
            html = fetch_page_html(browse_url)
            text = BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: could not fetch page text for tagging ({e}), tagging from the URL alone")
            text = url

        tags, summary, topic_area = self.analyze(text)
        log("GIT REPO:", repo_label)

        # No synthetic companion note - link straight to the clone's README.
        readme_link = browse = None
        if repo:
            stage("Cloning")
            clone_url = url if url.endswith(".git") else f"https://{host}/{owner}/{repo}.git"
            gitrepos.clone_repo(host, owner, repo, clone_url)
            # With a blob store the clone lives there and the vault folder
            # only gets the README (or the whole gist) plus a marker.
            vault_repo = blobs.mirror_repo(host, owner, repo)
            readme = gitrepos.find_gist_primary(vault_repo) if host == GIST_HOST else gitrepos.find_readme(vault_repo)
            if readme:
                readme_link = f"Dumps/git/{blobs.repo_rel(host, owner, repo)}/{readme.name}"
            browse = blobs.browse_link(host, owner, repo)

        # Local README and the live repo page are two different things - the
        # Area note is the only place either is recorded, so it gets both
        # (plus the stored clone's file listing when there is one).
        first = " · ".join(filter(None, [f"[[{readme_link}]]" if readme_link else None, browse]))
        source_line = f"{first}\n{url}" if first else url
        area_note, area_entry = self.write_entry(topic_area, tags, summary, source_line,
                                                 existing_area_note, existing_area_entry)
        return "Dumps/git", repo_label, area_note, area_entry

    def save_git_author(self, url, force: bool = False):
        """Clone every one of an author/org's repos. Owns its own dedupe:
        re-running an author URL later (to pick up new repos) should always
        re-check, while each repo is still cloned/tagged once - via save(),
        keyed the same as a direct repo URL."""
        host, raw_owner, _ = parse_git_repo_url(url)
        owner = safe_segment(raw_owner)
        if host != "github.com":
            raise SaveError(f"author/org-level download is only supported for github.com right now (got {host!r})")

        stage("Listing repositories")
        try:
            repos = gitrepos.fetch_github_repos(owner)
        except Exception as e:  # noqa: BLE001
            raise SaveError(f"could not list repos for {owner}: {e}") from e

        repos = [r for r in repos if not r.get("fork")]
        total = len(repos)
        log(f"AUTHOR: {owner} - {total} repo(s) found (forks excluded)")

        db = get_db()
        cloned = skipped = failed = 0
        for i, repo in enumerate(repos, 1):
            check_cancelled()
            repo_url = repo.get("html_url")
            full_name = repo.get("full_name") or repo_url
            if not repo_url:
                continue
            progress((i - 1) / max(total, 1), f"repo {i}/{total}: {full_name}")
            if db.has_entry(f"url:{normalize_url(repo_url)}") and not force:
                log(f"[{i}/{total}] {full_name}: already saved, skipping")
                skipped += 1
                continue
            log(f"[{i}/{total}] {full_name}: saving...")
            try:
                with span((i - 1) / total, i / total):
                    self.save(repo_url, force=force, report=False)
            except ContentFreeError as e:
                log(f"[{i}/{total}] {full_name}: skipped ({e})")
                failed += 1
                continue
            except JobCancelled:
                raise
            except Exception as e:  # noqa: BLE001 - one bad repo must not stop the scan
                log(f"[{i}/{total}] {full_name}: FAILED ({e})")
                failed += 1
                continue
            cloned += 1

        summary_line = f"{cloned} cloned, {skipped} already saved, {failed} failed ({owner})"
        log(f"Done: {summary_line}")
        return "Dumps/git", owner, summary_line

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------
    def save_pdf(self, url, existing_area_note=None, existing_area_entry=None, allow_thin=False,
                 data=None, existing_filename=None):
        """`data` short-circuits the download for a PDF already on disk
        (import_pdf_file). Everything downstream is identical either way, so
        a raw file and a fetched URL land in the vault in the same shape."""
        if data is None:
            stage("Downloading PDF")
            try:
                data = fetch_pdf(url)
            except Exception as e:  # noqa: BLE001
                raise SaveError(f"could not download PDF: {e}") from e

        stage("Extracting text")
        try:
            text = extract_pdf_text(data)
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: could not extract PDF text ({e})")
            text = ""
        if len(text.strip()) < PDF_TEXT_MIN_CHARS:
            # No text layer (a scan, an image export). Tagging from the
            # near-empty string would be the model confabulating, so use the
            # file's own metadata - framed, so the model describes the
            # document rather than the metadata it was shown.
            digest = pdf_metadata_digest(data)
            log(f"NOTE: no usable text layer in this PDF - tagging from "
                f"{'its metadata' if digest else 'the URL'} instead")
            text = (f"This document is a {pdf_page_count(data)}-page PDF with no extractable text "
                    f"(a scan or image export), so only its own embedded description is available. "
                    f"Describe the document itself, not this description.\n\n{digest}") if digest else url
        title = (extract_pdf_metadata_title(data) or llm.extract_title(text)
                 or Path(urlparse(url).path).stem or "untitled")

        tags, summary, topic_area = self.analyze(text, allow_thin=allow_thin)

        pdf_dir = Path(settings.pdf_dir)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        if existing_filename:
            # A force redo overwrites the copy it made last time rather than
            # orphaning it beside a "<title> (2).pdf".
            dest = pdf_dir / existing_filename
        else:
            dest = unique_destination(pdf_dir, f"{sanitize_filename(title)[:150]}.pdf")
        dest.write_bytes(data)

        source_line = f"[[Dumps/pdf/{dest.name}]]\n{url}"
        area_note, area_entry = self.write_entry(topic_area, tags, summary, source_line,
                                                 existing_area_note, existing_area_entry)
        return "Dumps/pdf", dest.name, area_note, area_entry

    # ------------------------------------------------------------------
    # video
    # ------------------------------------------------------------------
    def save_youtube(self, url, existing_area_note=None, existing_area_entry=None, allow_thin=False):
        video_id = extract_youtube_id(url) or "unknown"
        stage("Fetching video metadata")
        try:
            meta = media.fetch_youtube_metadata(url)
        except Exception as e:  # noqa: BLE001
            raise SaveError(f"could not fetch YouTube metadata: {e}") from e

        title = meta["title"]
        video_id = meta.get("id") or video_id

        safe_title = safe_segment(sanitize_filename(title)[:120])
        safe_channel = safe_segment(sanitize_filename(meta.get("uploader") or "unknown-channel")[:120])
        rel_dir = f"{safe_channel}/{safe_title} [{video_id}]"
        target_dir = blobs.media_work_dir("youtube", rel_dir)
        created = not target_dir.exists()

        log(f"Downloading video + transcript into {target_dir} ...")
        video_file = None
        try:
            video_file = media.download_youtube_video(url, target_dir)
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: video download failed ({e}) - the Area entry links the source URL instead. "
                "Retry the job once the cause (cookies, rate limit) is fixed.")

        # Filed after the download, so the transcript (when there is one)
        # decides what the video is about, not just its description.
        tags, summary, topic_area = self.analyze_download(
            video_text(f"Title: {title}\nChannel: {meta.get('uploader', '')}", meta.get("description", ""),
                       target_dir if video_file else None),
            allow_thin, target_dir, created)

        # No companion .md note - the folder of mkv + subtitles *is* the
        # artifact. Link the mkv; fall back to the URL if the download failed.
        if video_file:
            blobs.mirror_video("youtube", rel_dir, video_file)
        source_line = f"{blobs.video_link('youtube', rel_dir, video_file)}\n{url}" if video_file else url
        area_note, area_entry = self.write_entry(topic_area, tags, summary, source_line,
                                                 existing_area_note, existing_area_entry)
        return "Dumps/youtube", rel_dir, area_note, area_entry

    def save_ccc_talk(self, url, existing_area_note=None, existing_area_entry=None, allow_thin=False):
        """A media.ccc.de talk: same shape as save_youtube, laid out by
        conference rather than by channel."""
        stage("Fetching talk metadata")
        try:
            meta = media.fetch_ccc_metadata(url)
        except Exception as e:  # noqa: BLE001
            raise SaveError(f"could not fetch media.ccc.de metadata: {e}") from e

        title, talk_id = meta["title"], meta["id"] or "unknown"

        safe_title = safe_segment(sanitize_filename(title)[:120])
        safe_conf = safe_segment(sanitize_filename(meta["conference"] or "unknown-conference")[:120])
        rel_dir = f"{safe_conf}/{safe_title} [{talk_id}]"
        target_dir = blobs.media_work_dir("video", f"media.ccc.de/{rel_dir}")
        created = not target_dir.exists()

        size_gb = meta["filesize"] / 1e9 if meta["filesize"] else 0
        log(f"Downloading talk + transcript into {target_dir} ({meta['duration'] // 60} min"
            + (f", ~{size_gb:.1f} GB" if size_gb else "") + ") ...")
        video_file = None
        try:
            video_file = media.download_ccc_talk(url, target_dir)
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: talk download failed ({e}) - the Area entry links the source URL instead")

        tags, summary, topic_area = self.analyze_download(
            video_text(f"Title: {title}\nSpeakers: {meta['speakers']}\nConference: {meta['conference']}",
                       meta["description"], target_dir if video_file else None),
            allow_thin, target_dir, created)

        if video_file:
            blobs.mirror_video("video", f"media.ccc.de/{rel_dir}", video_file)
        source_line = (f"{blobs.video_link('video', f'media.ccc.de/{rel_dir}', video_file)}\n{url}"
                       if video_file else url)
        area_note, area_entry = self.write_entry(topic_area, tags, summary, source_line,
                                                 existing_area_note, existing_area_entry)
        return "Dumps/video", f"media.ccc.de/{rel_dir}", area_note, area_entry

    def save_youtube_channel(self, url, force: bool = False):
        """Owns its own dedupe: a channel URL isn't itself a thing to dedupe
        (re-running it to pick up new uploads is the point), only its
        videos are - each registered under the same key a direct
        single-video save would use."""
        stage("Listing channel videos")
        try:
            meta = media.fetch_youtube_channel_metadata(url)
        except Exception as e:  # noqa: BLE001
            raise SaveError(f"could not fetch YouTube channel metadata: {e}") from e

        title = meta["title"]
        entries = meta.get("entries", [])
        safe_channel = safe_segment(sanitize_filename(meta.get("uploader") or title)[:120])
        rel_dir = safe_channel
        # Channel videos all share one folder; with a blob store it lives
        # there (they're downloaded without subtitles, so nothing small is
        # worth keeping in the vault).
        target_dir = (blobs.blob_dir("youtube", rel_dir) if blobs.enabled()
                      else Path(settings.youtube_dir) / rel_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        total = len(entries)
        log(f"CHANNEL: {title} - {total} video(s) listed")
        db = get_db()
        downloaded = skipped = failed = 0
        new_ids = []
        stage("Downloading channel videos")
        for i, entry in enumerate(entries, 1):
            check_cancelled()
            vid = entry["id"]
            label = entry.get("title") or vid
            key = f"url:youtube.com/watch?v={vid}"
            if db.has_entry(key) and not force:
                log(f"[{i}/{total}] {label}: already saved, skipping")
                skipped += 1
                continue

            log(f"[{i}/{total}] {label}: downloading...")
            try:
                with span((i - 1) / total, i / total):
                    video_file = media.download_youtube_channel_video(vid, target_dir)
            except JobCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"[{i}/{total}] {label}: FAILED ({e})")
                failed += 1
                continue

            db.complete_entry(key, url=f"https://www.youtube.com/watch?v={vid}",
                              note=f"Dumps/youtube/{rel_dir}", job_id=_job_id())
            log(f"[{i}/{total}] {label}: done -> {video_file.name}")
            downloaded += 1
            new_ids.append(vid)

        summary_line = f"{downloaded} downloaded, {skipped} already saved, {failed} failed ({title})"
        log(f"Done: {summary_line}")
        if not new_ids:
            return "Dumps/youtube", rel_dir, summary_line

        text_for_llm = f"Channel: {title}\n\n{meta.get('description', '')}"
        tags, summary, topic_area = self.analyze(text_for_llm)
        channel_link = (f"[Channel videos]({blobs.url('youtube', rel_dir, directory=True)})"
                        if blobs.enabled() else f"[[Dumps/youtube/{rel_dir}]]")
        source_line = f"{channel_link}\n{url}"
        stage("Writing Area note")
        append_area_entry(topic_area, tags, summary, source_line)
        self.refresh_indexes()
        return "Dumps/youtube", rel_dir, summary_line

    # ------------------------------------------------------------------
    # files dropped in the inbox / uploaded
    # ------------------------------------------------------------------
    def import_captured_file(self, path: Path) -> str:
        """Import a SingleFile capture (browser extension + Companion, synced
        into the inbox, or uploaded through the UI). Recovers the source URL
        from the file's own header and routes through save() as normal.
        Afterwards the file is either consumed (renamed into the dump),
        deleted (a youtube/git/pdf handler fetched fresh and never needed
        it) or parked in _duplicate/ (already saved).

        Imports always run with allow_thin: a file only lands here because
        someone hit Save on that exact page, which is a stronger signal than
        the model's content-worthiness verdict."""
        html = path.read_text(encoding="utf-8", errors="ignore")
        url = extract_singlefile_source_url(html)
        if not url:
            raise SaveError("no SingleFile source URL found in file header")

        log(f"IMPORT: {path.name} -> {url}")
        try:
            outcome = self.save(url, precaptured=(html, path), allow_thin=True)
        except ContentFreeError as e:
            # Only reachable for the handlers allow_thin doesn't cover (git,
            # channel), where the verdict means the README/channel text came
            # back empty - the capture is the useless part, so drop it rather
            # than parking it for a retry that would fail identically.
            path.unlink(missing_ok=True)
            result(f"Dropped {path.name}: {e}")
            return "content_free"

        if not path.exists():
            return outcome
        if outcome == "dispatched_other":
            path.unlink()
            return outcome
        # skipped_dedupe on a generic page: save_generic never consumed the
        # file - move it aside so the inbox watcher doesn't re-flag it.
        _park_duplicate(path)
        return outcome

    def import_pdf_file(self, path: Path, source_url: str = None, force: bool = False) -> str:
        """Import a raw PDF that is already on disk. The bytes go straight to
        save_pdf rather than a URL being re-fetched - the local copy may be
        the only one left. `source_url` records the real origin when known;
        otherwise a local:// stand-in is synthesized. Runs with allow_thin
        for the same reason captures do."""
        data = path.read_bytes()
        if not looks_like_pdf(data):
            raise SaveError(f"{path.name} is not a PDF (no %PDF- header)")

        # A file inside pdf_dir is the vault's own copy being re-imported (a
        # force redo), not a new drop: never compare it to itself or delete it.
        in_pdf_dir = Path(settings.pdf_dir).resolve() in path.resolve().parents

        stage("Checking for an identical PDF")
        twin = find_identical_pdf(data, ignoring=path)
        if twin and not force:
            log(f"IMPORT: {path.name} is byte-identical to {twin.name}, already in the vault "
                f"- not saving it twice (use force to override)")
            if not in_pdf_dir:
                _park_duplicate(path)
            result(f"Already in the vault -> Dumps/pdf/{twin.name}")
            return "skipped_dedupe"

        url = source_url or pdf_import_source_url(data, path)
        log(f"IMPORT: {path.name} -> {url}")
        try:
            outcome = self.save(url, force=force, precaptured_pdf=data, allow_thin=True)
        except ContentFreeError as e:
            # Leave the file alone: unlike a stale web capture, this local
            # copy may be irreplaceable.
            result(f"Left {path.name} in place: {e}")
            return "content_free"

        if in_pdf_dir:
            return outcome
        if outcome == "skipped_dedupe":
            _park_duplicate(path)
            return outcome
        # save_pdf wrote its own copy into pdf_dir; the upload is redundant.
        path.unlink(missing_ok=True)
        return outcome

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    # Text any forge's repo page carries (its software's name, a clone URL),
    # and almost no ordinary article does. Pages without it skip the LLM.
    FORGE_MARKERS = re.compile(
        r"gitea|forgejo|gitlab|gogs|sourcehut|sr\.ht|cgit|gitweb|onedev|git clone|\.git\b", re.IGNORECASE)

    def detect_git_repo(self, url: str, html: str):
        """Heuristic fallback for repos on forges outside GIT_HOSTS (a
        self-hosted Gitea/Forgejo/GitLab) whose URL doesn't end in .git.
        The host is the page's own netloc, never something the LLM invents
        (it once fabricated a github.com URL for a Gitea repo), and the
        reconstructed URL must resolve before it is trusted.

        Only pages showing a forge marker are put to the LLM: the question
        costs a full model call (and, on a cold Ollama, the model load)
        for every saved article otherwise."""
        if not self.FORGE_MARKERS.search(html):
            return None, None, None
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = soup.get_text(separator="\n", strip=True)[:2000]

        stage("Checking whether this is a git repo")
        parsed = llm.detect_git_repo_prompt(url, title, text)
        if not parsed or not parsed.get("is_repo"):
            return None, None, None
        owner, repo = parsed.get("owner"), parsed.get("repo")
        if not owner or not repo:
            return None, None, None

        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        try:
            response = requests.head(f"https://{host}/{owner}/{repo}", timeout=10,
                                     headers={"User-Agent": settings.http_user_agent}, allow_redirects=True)
            if response.status_code != 200:
                log(f"HEURISTIC: {url} looked like a repo at {host}/{owner}/{repo} but that URL "
                    f"returned {response.status_code} - treating as a generic page instead")
                return None, None, None
        except requests.RequestException as e:
            log(f"HEURISTIC: could not verify {host}/{owner}/{repo} ({e}) - treating as a generic page instead")
            return None, None, None
        return host, owner, repo

    def save(self, url: str, force: bool = False, precaptured=None, allow_thin: bool = False,
             precaptured_pdf: bytes = None, report: bool = True) -> str:
        """Returns an outcome string - "consumed" (save_generic used
        precaptured), "dispatched_other" (youtube/git/pdf handled it,
        precaptured untouched), or "skipped_dedupe" (already saved).

        `precaptured_pdf` pins the save to the PDF handler outright: its url
        may be a synthesized local:// stand-in no probing could classify.

        report=False for nested saves (an author scan's repos) so they don't
        overwrite the parent job's result line."""
        say = result if report else log

        if precaptured_pdf is None and is_youtube_channel_url(url):
            note_root, area, summary_line = self.save_youtube_channel(url, force=force)
            say(f"Channel done -> {note_root}/{area}: {summary_line}")
            return "dispatched_other"

        if precaptured_pdf is None and is_git_author_url(url):
            note_root, area, summary_line = self.save_git_author(url, force=force)
            say(f"Author done -> {note_root}/{area}: {summary_line}")
            return "dispatched_other"

        normalized = normalize_url(url)
        if not normalized:
            raise SaveError(f"could not parse URL: {url}")
        key = f"url:{normalized}"

        db = get_db()
        state, existing = db.claim_entry(key, url, job_id=_job_id(), force=force)
        if state == "exists":
            say(f"Already saved -> {existing.get('note')} (on {existing.get('processed_at')})")
            return "skipped_dedupe"
        if state == "in_progress":
            say(f"Already being saved by another job -> {url}")
            return "skipped_dedupe"
        prior = existing or {}
        prior_area_note = prior.get("area_note") if force else None
        prior_area_entry = prior.get("area_entry") if force else None
        prior_filename = prior.get("filename") if force else None

        try:
            filename = None
            outcome = "dispatched_other"
            if precaptured_pdf is not None:
                note_root, area, area_note, area_entry = self.save_pdf(
                    url, existing_area_note=prior_area_note, existing_area_entry=prior_area_entry,
                    allow_thin=allow_thin, data=precaptured_pdf, existing_filename=prior_filename)
                # PDFs record their filename so a later force can overwrite it.
                filename = area
            elif is_youtube_url(url):
                note_root, area, area_note, area_entry = self.save_youtube(
                    url, existing_area_note=prior_area_note, existing_area_entry=prior_area_entry,
                    allow_thin=allow_thin)
            elif is_ccc_talk_url(url):
                note_root, area, area_note, area_entry = self.save_ccc_talk(
                    url, existing_area_note=prior_area_note, existing_area_entry=prior_area_entry,
                    allow_thin=allow_thin)
            elif parse_git_repo_url(url)[1]:
                note_root, area, area_note, area_entry = self.save_git_repo(
                    url, existing_area_note=prior_area_note, existing_area_entry=prior_area_entry)
            elif is_pdf_url(url):
                note_root, area, area_note, area_entry = self.save_pdf(
                    url, existing_area_note=prior_area_note, existing_area_entry=prior_area_entry,
                    allow_thin=allow_thin, existing_filename=prior_filename)
                filename = area
            else:
                heuristic_html = precaptured[0] if precaptured else None
                if heuristic_html is None:
                    try:
                        heuristic_html = fetch_page_html(url)
                    except Exception:  # noqa: BLE001
                        heuristic_html = None
                host = owner = repo = None
                if heuristic_html:
                    host, owner, repo = self.detect_git_repo(url, heuristic_html)

                if owner and repo:
                    owner, repo = safe_segment(owner), safe_segment(repo)
                    log(f"HEURISTIC: {url} looks like a git repo ({host}/{owner}/{repo}) - saving as one")
                    note_root, area, area_note, area_entry = self.save_git_repo(
                        f"https://{host}/{owner}/{repo}", host=host, owner=owner, repo=repo,
                        existing_area_note=prior_area_note, existing_area_entry=prior_area_entry)
                else:
                    note_root, area, filename, area_note, area_entry = self.save_generic(
                        url, precaptured=precaptured, existing_filename=prior_filename,
                        existing_area_note=prior_area_note, existing_area_entry=prior_area_entry,
                        allow_thin=allow_thin)
                    outcome = "consumed"
        except BaseException:
            # Release the claim rather than leaving it stuck "in progress"
            # forever (or, for a force redo, restore the previous record).
            db.release_claim(key)
            raise

        note = f"{note_root}/{area}" if area else note_root
        db.complete_entry(key, url=url, note=note, filename=filename, area_note=area_note,
                          area_entry=area_entry, job_id=_job_id())
        final_line = f"Saved -> {note}"
        if area_note and note_root != "Areas":
            # Only worth spelling out for git/pdf/video, whose primary
            # location is the clone/file/video, not the Area note.
            final_line += f" (tagged under Areas/{area_note})"
        say(final_line)
        return outcome
