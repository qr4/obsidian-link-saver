"""URL classification and normalization."""
import re
from urllib.parse import parse_qs, urlparse

import requests

from .config import settings


def extract_youtube_id(url: str):
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc in ("youtu.be", "www.youtu.be"):
        return parsed.path.strip("/").split("/")[0] or None
    if "youtube.com" in netloc:
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        match = re.match(r"^/(shorts|embed|live)/([^/]+)", parsed.path)
        if match:
            return match.group(2)
    return None


def is_youtube_url(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc in ("youtu.be", "www.youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com")


def is_youtube_channel_url(url: str) -> bool:
    """True for a channel/user/handle/playlist URL - any youtube.com link
    that isn't a single video (extract_youtube_id finds no id). Covers
    /@handle, /channel/UC.../c/name, /user/name and bare ?list= links."""
    return is_youtube_url(url) and extract_youtube_id(url) is None


def is_pdf_url(url: str) -> bool:
    """A literal .pdf extension is the fast/common case and needs no
    network round-trip. Some sites - arXiv's current /pdf/<id> link scheme
    among them - serve PDFs with no file extension at all, so a cheap HEAD
    request checks the real Content-Type as a fallback before the URL falls
    through to being treated as a generic HTML page."""
    if urlparse(url).path.lower().endswith(".pdf"):
        return True
    try:
        response = requests.head(url, timeout=10, headers={"User-Agent": settings.http_user_agent},
                                 allow_redirects=True)
        return response.headers.get("Content-Type", "").lower().startswith("application/pdf")
    except requests.RequestException:
        return False


def is_ccc_url(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc in ("media.ccc.de", "www.media.ccc.de")


def is_ccc_talk_url(url: str) -> bool:
    """True only for a single talk (/v/<slug>). media.ccc.de also serves
    conference and tag indexes (/c/39c3, /b/..., /tags/...) which yt-dlp
    happily expands - /c/39c3 alone is 180 talks - so everything that isn't
    /v/ is left to the normal page capture rather than risking a bulk
    download nobody asked for."""
    return is_ccc_url(url) and urlparse(url).path.startswith("/v/")


GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht", "gitea.tendokyu.moe"}

# Deliberately not in GIT_HOSTS - gist paths need their own shape check, see
# parse_git_repo_url. Gist ids are 20-32 hex chars, which is what separates
# /<id> (an anonymous gist) from /<user> (a user's gist index page).
GIST_HOST = "gist.github.com"
GIST_ID_RE = re.compile(r"^[0-9a-f]{20,}$", re.I)


def parse_git_repo_url(url: str):
    """Detect a git-hosting URL and pull out (host, owner, repo).

    Two ways in: a known host's /owner/repo path, or - regardless of host,
    covering self-hosted GitLab/Gitea/etc. a fixed hostname list can't -
    any URL whose path ends in '.git'. Returns (None, None, None) when
    neither matches."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path

    if path.endswith(".git"):
        parts = [p for p in path.split("/") if p]
        repo = parts[-1][:-4] if parts else None
        owner = parts[-2] if len(parts) >= 2 else None
        return netloc, owner, repo

    if netloc == GIST_HOST:
        # Gists are git repos, but their paths don't follow /owner/repo: an
        # anonymous gist is just /<id>, and /<user> alone is that user's gist
        # index. Handled here instead of via GIT_HOSTS because the generic
        # branch below would read both single-segment paths as an owner with
        # no repo - i.e. an author URL - and kick off an org-wide repo scan
        # for a hex string.
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and GIST_ID_RE.match(parts[1]):
            return netloc, parts[0], parts[1]
        if len(parts) == 1 and GIST_ID_RE.match(parts[0]):
            # GitHub resolves a gist by id whatever owner segment precedes it,
            # so a placeholder keeps the host/owner/repo layout intact.
            return netloc, "anonymous", parts[0]
        return None, None, None

    if netloc not in GIT_HOSTS:
        return None, None, None
    parts = [p for p in path.split("/") if p]
    owner = parts[0] if len(parts) >= 1 else None
    repo = parts[1] if len(parts) >= 2 else None
    return netloc, owner, repo


def is_git_author_url(url: str) -> bool:
    """True for a bare owner/org profile URL on a known git host (e.g.
    github.com/torvalds) as opposed to a link to one specific repo."""
    host, owner, repo = parse_git_repo_url(url)
    return bool(host and owner and not repo)


def normalize_url(url: str):
    """Canonicalize a URL for duplicate detection: strips scheme/www noise
    and collapses YouTube's many share-link forms (youtu.be, ?si=, /shorts/)
    down to a single youtube.com/watch?v=<id> form."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()

    video_id = extract_youtube_id(url)
    if video_id:
        return f"youtube.com/watch?v={video_id}"

    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    if netloc in GIT_HOSTS or netloc == GIST_HOST:
        path = _canonical_repo_path(path)
    return f"{netloc}{path}"


def _canonical_repo_path(path: str) -> str:
    """Git hosts treat owner and repo names case-insensitively
    (github.com/SigNoz/signoz is github.com/signoz/signoz), and `.git` is
    optional on a repo URL. So both variants must give the same dedupe key,
    or the repo is saved twice into folders that differ only in case; on a
    case-insensitive sync target (LiveSync) those collide. Only the first two
    segments are folded: paths inside a repo (/blob/main/File.md) stay
    case-sensitive."""
    parts = path.split("/")  # ["", owner, repo, ...]
    for i in (1, 2):
        if i < len(parts):
            parts[i] = parts[i].lower()
    if len(parts) == 3 and parts[2].endswith(".git"):
        parts[2] = parts[2][:-4]
    return "/".join(parts)


def dedupe_key(url: str):
    normalized = normalize_url(url)
    return f"url:{normalized}" if normalized else None


def classify(url: str) -> str:
    """Cheap, network-free label for the UI ("what will this become?")."""
    if is_youtube_channel_url(url):
        return "youtube-channel"
    if is_youtube_url(url):
        return "youtube"
    if is_ccc_talk_url(url):
        return "video"
    if is_git_author_url(url):
        return "git-author"
    if parse_git_repo_url(url)[1]:
        return "git"
    if urlparse(url).path.lower().endswith(".pdf"):
        return "pdf"
    return "page"
