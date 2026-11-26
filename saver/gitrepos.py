"""Git clones and GitHub author listing."""
import os
import shutil
import subprocess
from pathlib import Path

import requests

from . import proc
from .config import settings
from .jobctx import log, progress


def fetch_github_repos(owner: str):
    """All of owner's repos via the GitHub REST API: /users first (personal
    accounts), falling back to /orgs since GitHub 404s /users/{name}/repos
    for some organizations. Unauthenticated, so subject to the 60 req/hr
    limit - fine for listing one author (100 per page)."""
    for path in (f"users/{owner}/repos", f"orgs/{owner}/repos"):
        repos = []
        page = 1
        while True:
            response = requests.get(
                f"https://api.github.com/{path}",
                params={"per_page": 100, "page": page},
                headers={"User-Agent": settings.http_user_agent},
                timeout=30,
            )
            if response.status_code == 404:
                repos = None
                break
            response.raise_for_status()
            batch = response.json()
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        if repos is not None:
            return repos
    raise RuntimeError(f"no GitHub user or org named {owner!r}")


def repo_dest_dir(host: str, owner: str, repo: str) -> Path:
    """<root>/<host>/<owner>/<repo>, uniform across every git host so an
    owner/repo pair can never collide across hosts. The root is the blob
    store when one is configured (the vault then only gets the README via
    blobs.mirror_repo), else Dumps/git."""
    from . import blobs
    return blobs.repo_clone_dir(host, owner, repo)


def clone_repo(host: str, owner: str, repo: str, clone_url: str):
    """Shallow-clone a repo into repo_dest_dir(...) if not already present.

    Claims dest via a bare mkdir() before cloning so two concurrent saves
    of the same repo can't both `git clone` into it. On failure (including a
    timeout, which would otherwise leave a HEAD-less skeleton that looks
    "already cloned" forever) the directory is removed again. A directory
    without .git/HEAD is treated as such a stub and re-cloned."""
    dest = repo_dest_dir(host, owner, repo)
    if dest.exists():
        # The offload moved the full clone to the blob store and left only
        # the README(s) plus this marker - a finished clone, not a stub.
        if (dest / ".offloaded").exists():
            log(f"Repo already cloned and offloaded to the blob store ({dest / '.offloaded'}), skipping")
            return
        if (dest / ".git" / "HEAD").exists():
            log(f"Repo already cloned at {dest}, skipping")
            return
        log(f"WARNING: {dest} exists but looks like an incomplete clone (no HEAD) - re-cloning")
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.mkdir()
    except FileExistsError:
        log(f"Repo already being cloned at {dest} by another job, skipping")
        return
    progress(None, f"cloning {host}/{owner}/{repo}")
    try:
        # GIT_TERMINAL_PROMPT=0: a deleted/private repo fails instead of
        # waiting forever for credentials
        result = proc.run(["git", "clone", "--depth", "1", clone_url, str(dest)], timeout=300,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        log(f"WARNING: git clone timed out for {clone_url}")
        shutil.rmtree(dest, ignore_errors=True)
        return
    except BaseException:
        # cancelled mid-clone: never leave a HEAD-less stub that looks cloned
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if result.returncode != 0:
        log(f"WARNING: git clone failed for {clone_url}:\n{result.stderr[-2000:]}")
        shutil.rmtree(dest, ignore_errors=True)
        return
    from . import blobs
    blobs.invalidate_usage()  # a new clone in the store (no-op cost without one)


README_NAMES = ("README.md", "Readme.md", "readme.md", "README.MD", "README.rst",
                "README.org", "readme.org", "README.txt", "README")


def find_readme(repo_dir: Path):
    for name in README_NAMES:
        candidate = repo_dir / name
        if candidate.exists():
            return candidate
    return None


def find_gist_primary(repo_dir: Path):
    """The content file of a cloned gist. Gists almost never carry a README -
    the payload is one well-named file - so fall back to the largest tracked
    file, which for a multi-file gist is the one worth opening first."""
    readme = find_readme(repo_dir)
    if readme:
        return readme
    if not repo_dir.is_dir():
        return None
    files = [p for p in repo_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    return max(files, key=lambda p: p.stat().st_size) if files else None
