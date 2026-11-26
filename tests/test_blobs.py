"""Offsite blob store: saving into it, offloading old dumps, serving it."""
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saver import blobs, gitrepos, media, pipeline
from saver.blobserve import content_type, srt_to_vtt
from saver.config import settings
from saver.pipeline import Saver

PUBLIC = "https://saver.test"
SRT = "1\n00:00:01,000 --> 00:00:02,500\nhello\n"


@pytest.fixture
def store(env, tmp_path):
    root = tmp_path / "blobs"
    root.mkdir()
    settings.update(blob_dir=str(root), public_url=PUBLIC, offload_min_age=600)
    yield root
    settings.update(blob_dir="", public_url="")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def make_video_dir(directory: Path, stem="Talk [abc123def45]"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.mkv").write_bytes(os.urandom(256 * 1024))
    (directory / "out.srt").write_text(SRT)
    (directory / "out.txt").write_text("hello\n")
    (directory / "out.json").write_text("{}")
    (directory / f"{stem}.en-orig.srt").write_text(SRT)
    return directory / f"{stem}.mkv"


def age(path: Path, seconds=3600):
    old = time.time() - seconds
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (old, old), follow_symlinks=False)


# ---------------------------------------------------------------------------
# saving with a store
# ---------------------------------------------------------------------------
def test_youtube_save_goes_to_the_store(store, env, monkeypatch):
    monkeypatch.setattr(media, "fetch_youtube_metadata", lambda url: {
        "id": "abc123def45", "title": "Talk", "uploader": "Chan", "description": "about things"})
    seen = {}

    def fake_download(url, target_dir):
        seen["dir"] = target_dir
        return make_video_dir(target_dir)

    monkeypatch.setattr(media, "download_youtube_video", fake_download)
    Saver().save("https://www.youtube.com/watch?v=abc123def45")

    rel = "Chan/Talk [abc123def45]"
    assert seen["dir"] == store / "youtube" / rel
    vault_dir = env["vault"] / "Dumps" / "youtube" / rel
    assert sorted(p.name for p in vault_dir.iterdir()) == [
        ".offloaded", "Talk [abc123def45].en-orig.srt", "Talk [abc123def45].offloaded",
        "out.json", "out.srt", "out.txt"]
    watch = f"{PUBLIC}/watch/youtube/Chan/Talk%20%5Babc123def45%5D/Talk%20%5Babc123def45%5D.mkv"
    assert (vault_dir / ".offloaded").read_text().strip() == watch
    assert (vault_dir / "Talk [abc123def45].offloaded").read_text().strip() == watch
    assert (store / "youtube" / rel / "Talk [abc123def45].mkv").exists()
    entry = env["db"].get_entry("url:youtube.com/watch?v=abc123def45")
    assert f"[▶ Watch]({watch}) · [[Dumps/youtube/{rel}/out.txt|Transcript]]" in entry["area_entry"]
    assert entry["note"] == f"Dumps/youtube/{rel}"


def fake_clone_into(dest: Path):
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (dest / "README.md").write_text("# Tool\n")
    (dest / "src").mkdir()
    (dest / "src" / "main.c").write_text("int main(){}\n")


def test_git_save_keeps_readme_in_vault(store, env, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_page_html", lambda url: "<html><body>A tool</body></html>")
    monkeypatch.setattr(gitrepos, "clone_repo",
                        lambda host, owner, repo, url: fake_clone_into(gitrepos.repo_dest_dir(host, owner, repo)))
    Saver().save("https://github.com/someone/tool")

    vault_repo = env["vault"] / "Dumps" / "git" / "github.com" / "someone" / "tool"
    assert sorted(p.name for p in vault_repo.iterdir()) == [".offloaded", "README.md"]
    listing = f"{PUBLIC}/blobs/git/github.com/someone/tool/"
    assert (vault_repo / ".offloaded").read_text().strip() == listing
    assert (store / "git" / "github.com" / "someone" / "tool" / "src" / "main.c").exists()
    entry = env["db"].get_entry("url:github.com/someone/tool")
    assert f"[[Dumps/git/github.com/someone/tool/README.md]] · [Browse files]({listing})" in entry["area_entry"]


def test_without_public_url_everything_stays_in_the_vault(env, tmp_path, monkeypatch):
    settings.update(blob_dir=str(tmp_path / "blobs"), public_url="")
    try:
        assert not blobs.enabled()
        assert blobs.media_work_dir("youtube", "C/V") == env["vault"] / "Dumps" / "youtube" / "C" / "V"
        assert blobs.repo_clone_dir("github.com", "a", "b") == env["vault"] / "Dumps" / "git" / "github.com" / "a" / "b"
        assert blobs.video_link("youtube", "C/V", Path("x.mkv")) == "[[Dumps/youtube/C/V/x.mkv]]"
    finally:
        settings.update(blob_dir="")


def test_in_vault_video_is_reused(store, env):
    in_vault = env["vault"] / "Dumps" / "youtube" / "C" / "V"
    make_video_dir(in_vault)
    assert blobs.media_work_dir("youtube", "C/V") == in_vault


# ---------------------------------------------------------------------------
# offload
# ---------------------------------------------------------------------------
def make_upstream(path: Path, files: dict, symlinks: dict = None) -> str:
    """A local git repo to clone from; returns its file:// URL (shallow
    clones need a URL, not a plain path)."""
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    for name, target in (symlinks or {}).items():
        os.symlink(target, path / name)
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    return path.as_uri()


def build_unstored_vault(vault: Path, upstreams: Path):
    """A vault saved without a blob store: videos and full clones in Dumps/."""
    dumps = vault / "Dumps"
    video = make_video_dir(dumps / "youtube" / "Chan" / "Talk [abc123def45]")
    repo_url = make_upstream(upstreams / "r", {"README.md": "# r\n", "src/main.c": "int main(){}\n",
                                               "COPYING": "license text\n"}, {"LICENSE": "COPYING"})
    repo = dumps / "git" / "github.com" / "o" / "r"
    git("clone", "-q", "--depth", "1", repo_url, str(repo))
    gist_url = make_upstream(upstreams / "gist", {"notes.md": "gist body\n"})
    gist = dumps / "git" / "gist.github.com" / "u" / "0123456789abcdef0123"
    git("clone", "-q", "--depth", "1", gist_url, str(gist))
    age(dumps)
    fresh = make_video_dir(dumps / "youtube" / "Chan" / "New [zzzzzzzzzzz]", "New [zzzzzzzzzzz]")
    note = vault / "Areas" / "programming" / "talks.md"
    note.write_text(f"- #talks\n\tA talk.\n\t[[Dumps/youtube/Chan/Talk [abc123def45]/{video.name}]]\n\thttps://y\n")
    return video, repo, gist, fresh, note


def test_offload_dry_run_changes_nothing(store, env, tmp_path):
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    before = note.read_text()
    plans = blobs.offload(apply=False)
    assert [p.name for p in plans["youtube"]["bulk"]] == ["Talk [abc123def45].mkv"]
    assert "New [zzzzzzzzzzz].mkv" in [p.name for p in plans["youtube"]["skipped"]]  # too new
    assert sorted(str(r) for r in plans["git"]["repos"]) == [
        "gist.github.com/u/0123456789abcdef0123", "github.com/o/r"]
    assert video.exists() and (repo / ".git").is_dir() and note.read_text() == before
    assert not any(store.iterdir())


def test_offload_moves_videos_and_reclones_repos(store, env, tmp_path):
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    video_bytes = video.read_bytes()
    blobs.offload(apply=True)

    rel = "Chan/Talk [abc123def45]"
    stored = store / "youtube" / rel / video.name
    assert stored.read_bytes() == video_bytes and not video.exists()
    vault_dir = env["vault"] / "Dumps" / "youtube" / rel
    assert (vault_dir / "out.txt").exists() and (vault_dir / "out.srt").exists()
    watch = (vault_dir / ".offloaded").read_text().strip()
    assert watch.startswith(f"{PUBLIC}/watch/youtube/")
    assert f"[▶ Watch]({watch}) · [[Dumps/youtube/{rel}/out.txt|Transcript]]" in note.read_text()
    assert fresh.exists()  # too new to move

    # the repo was re-cloned (committed symlink intact), the vault keeps README + marker
    clone = store / "git" / "github.com" / "o" / "r"
    assert (clone / ".git" / "HEAD").exists() and (clone / "src" / "main.c").exists()
    assert os.readlink(clone / "LICENSE") == "COPYING"
    assert sorted(p.name for p in repo.iterdir()) == [".offloaded", "README.md"]
    assert (repo / ".offloaded").read_text().strip() == f"{PUBLIC}/blobs/git/github.com/o/r/"
    assert sorted(p.name for p in gist.iterdir()) == [".offloaded", "notes.md"]  # gists stay whole
    assert (store / "git" / "gist.github.com" / "u" / "0123456789abcdef0123" / ".git").is_dir()
    assert not list(store.rglob("*.cloning"))

    # idempotent: a second run finds nothing left to move
    plans = blobs.offload(apply=False)
    assert all(not p["bulk"] for p in plans.values()) and not plans["git"]["repos"]


def test_repo_files_with_download_suffixes_are_not_in_progress(store, env, tmp_path):
    """genesis-world ships assets/part_1.part - a CAD part, not a partial download."""
    url_ = make_upstream(tmp_path / "up" / "g", {"README.md": "# g\n", "assets/part_1.part": "solid\n"})
    repo = env["vault"] / "Dumps" / "git" / "github.com" / "g" / "genesis"
    git("clone", "-q", "--depth", "1", url_, str(repo))
    age(repo)
    plans = blobs.offload(apply=False, categories=("git",))
    assert plans["git"]["repos"][Path("github.com/g/genesis")]["blocked"] is None


def test_repo_that_cannot_be_recloned_stays(store, env, tmp_path):
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    git("remote", "set-url", "origin", (tmp_path / "gone").as_uri(), cwd=repo)
    age(repo)
    blobs.offload(apply=True)
    assert (repo / ".git" / "HEAD").exists() and (repo / "src" / "main.c").exists()
    assert not (store / "git" / "github.com" / "o" / "r").exists()
    assert not (gist / ".git").exists()  # the other repo still moved


def test_repo_already_in_store_is_just_removed_from_vault(store, env, tmp_path):
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    git("remote", "set-url", "origin", (tmp_path / "gone").as_uri(), cwd=repo)  # would fail to clone
    age(repo)
    existing = store / "git" / "github.com" / "o" / "r"
    fake_clone_into(existing)
    blobs.offload(apply=True)
    assert sorted(p.name for p in repo.iterdir()) == [".offloaded", "README.md"]
    assert (existing / "src" / "main.c").exists()


def test_recently_changed_repo_is_held_back_whole(store, env, tmp_path):
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    (repo / "src" / "main.c").write_text("edited just now\n")
    plans = blobs.offload(apply=True)
    assert plans["git"]["repos"][Path("github.com/o/r")]["blocked"] == "changed recently: src/main.c"
    assert (repo / ".git" / "HEAD").exists() and (repo / "COPYING").exists()


def test_offload_channel_folder_gets_per_video_markers(store, env):
    channel = env["vault"] / "Dumps" / "youtube" / "Chan"
    channel.mkdir(parents=True)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        (channel / f"Video [{vid}].mkv").write_bytes(os.urandom(1024))
        (channel / f"Video [{vid}].en.srt").write_text(SRT)
    age(channel)
    blobs.offload(apply=True)
    assert not (channel / ".offloaded").exists()  # two videos: no folder marker
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        marker = (channel / f"Video [{vid}].offloaded").read_text().strip()
        assert marker.endswith(f"Video%20%5B{vid}%5D.mkv")
        assert (store / "youtube" / "Chan" / f"Video [{vid}].mkv").exists()
        assert (channel / f"Video [{vid}].en.srt").exists()


def test_offload_conflict_is_left_in_place(store, env, tmp_path):
    video, *_ = build_unstored_vault(env["vault"], tmp_path / "up")
    clash = store / "youtube" / "Chan" / "Talk [abc123def45]" / video.name
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"different")
    blobs.offload(apply=True)
    assert video.exists() and clash.read_bytes() == b"different"


def test_offload_requires_configuration(env):
    with pytest.raises(RuntimeError):
        blobs.offload()


# ---------------------------------------------------------------------------
# serving
# ---------------------------------------------------------------------------
@pytest.fixture
def client(store, env):
    from saver.app import create_app
    return TestClient(create_app(run_lifecycle=False))


def test_listing_files_and_ranges(client, store):
    video = make_video_dir(store / "youtube" / "Chan" / "Talk [abc123def45]")
    listing = client.get("/blobs/youtube/Chan/")
    assert listing.status_code == 200 and "Talk [abc123def45]/" in listing.text
    assert client.get("/blobs/youtube/Chan", follow_redirects=False).status_code in (301, 302, 307)
    folder = client.get("/blobs/youtube/Chan/Talk%20%5Babc123def45%5D/")
    assert "▶ Watch" in folder.text and ".offloaded" not in folder.text

    url = "/blobs/youtube/Chan/Talk%20%5Babc123def45%5D/Talk%20%5Babc123def45%5D.mkv"
    full = client.get(url)
    assert full.headers["content-type"] == "video/x-matroska" and full.content == video.read_bytes()
    assert full.headers["content-security-policy"] == "sandbox"
    part = client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == video.read_bytes()[100:200]


def test_no_escape_from_the_store(client, store, env):
    (env["config"] / "secret.txt").write_text("nope")
    (store / "git").mkdir()
    os.symlink(env["config"] / "secret.txt", store / "git" / "link.txt")
    assert client.get("/blobs/../config/secret.txt").status_code == 404
    assert client.get("/blobs/%2e%2e/config/secret.txt").status_code == 404
    assert client.get("/blobs/git/link.txt").status_code == 404


def test_watch_page_and_subtitles(client, store):
    make_video_dir(store / "youtube" / "Chan" / "Talk [abc123def45]")
    page = client.get("/watch/youtube/Chan/Talk%20%5Babc123def45%5D/Talk%20%5Babc123def45%5D.mkv")
    assert page.status_code == 200
    assert 'label="Whisper"' in page.text and 'label="YouTube (en-orig)"' in page.text
    assert page.text.count("<track") == 2  # out.srt once, not again as out.vtt
    vtt = client.get("/subs/youtube/Chan/Talk%20%5Babc123def45%5D/out.srt")
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert vtt.text.startswith("WEBVTT\n\n") and "00:00:01.000 --> 00:00:02.500" in vtt.text
    assert client.get("/watch/youtube/Chan/Talk%20%5Babc123def45%5D/out.txt").status_code == 404


def test_content_types(tmp_path):
    (tmp_path / "lib.rs").write_text("fn main() {}\n")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    assert content_type(tmp_path / "lib.rs") == "text/plain; charset=utf-8"
    assert content_type(tmp_path / "blob.bin") == "application/octet-stream"
    assert content_type(tmp_path / "pic.png") == "image/png"
    assert srt_to_vtt("﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nhi\r\n").startswith("WEBVTT\n\n1\n00:00:01.000")


def test_storage_api(client):
    status = client.get("/api/storage").json()
    assert status["enabled"] is True and status["free"] > 0
    job = client.post("/api/storage/update-repos").json()
    assert job["status"] == "queued"
    assert client.post("/api/storage/update-repos").json()["status"] == "already_queued"
    assert client.post("/api/storage/offload").status_code in (404, 405)  # one-time migration: CLI only


# ---------------------------------------------------------------------------
# repo updates (real git)
# ---------------------------------------------------------------------------
def test_update_repos_pulls_and_refreshes_readme(store, env, tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "README.md").write_text("v1\n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "v1", cwd=upstream)
    clone = store / "git" / "example.org" / "me" / "proj"
    clone.parent.mkdir(parents=True)
    git("clone", "-q", str(upstream), str(clone))
    blobs.mirror_repo("example.org", "me", "proj")
    (upstream / "README.md").write_text("v2\n")
    git("commit", "-q", "-am", "v2", cwd=upstream)

    blobs.update_repos()
    assert (clone / "README.md").read_text() == "v2\n"
    assert (env["vault"] / "Dumps" / "git" / "example.org" / "me" / "proj" / "README.md").read_text() == "v2\n"


def _clone_with_update(store, tmp_path):
    """A stored clone whose upstream has one new commit touching run.sh."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "README.md").write_text("v1\n")
    (upstream / "run.sh").write_text("echo v1\n")
    os.chmod(upstream / "run.sh", 0o755)
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "v1", cwd=upstream)
    clone = store / "git" / "example.org" / "me" / "proj"
    clone.parent.mkdir(parents=True)
    git("clone", "-q", "--depth", "1", f"file://{upstream}", str(clone))
    (upstream / "run.sh").write_text("echo v2\n")
    git("commit", "-q", "-am", "v2", cwd=upstream)
    return upstream, clone


def _stashes(clone):
    return subprocess.run(["git", "-C", str(clone), "stash", "list"],
                          capture_output=True, text=True, check=True).stdout


def test_update_repos_ignores_mode_only_changes(store, env, tmp_path):
    _, clone = _clone_with_update(store, tmp_path)
    os.chmod(clone / "run.sh", 0o644)  # what a store without the x bit does

    blobs.update_repos()
    assert (clone / "run.sh").read_text() == "echo v2\n"
    assert _stashes(clone) == ""


def test_update_repos_stashes_blocking_edits(store, env, tmp_path):
    _, clone = _clone_with_update(store, tmp_path)
    (clone / "run.sh").write_text("echo mine\n")
    (clone / "notes.txt").write_text("untracked\n")

    blobs.update_repos()
    assert (clone / "run.sh").read_text() == "echo v2\n"
    assert (clone / "notes.txt").read_text() == "untracked\n"
    shown = subprocess.run(["git", "-C", str(clone), "stash", "show", "-p"],
                           capture_output=True, text=True, check=True).stdout
    assert "+echo mine" in shown


def test_update_repos_leaves_edits_alone_without_update(store, env, tmp_path):
    _, clone = _clone_with_update(store, tmp_path)
    blobs.update_repos()
    (clone / "run.sh").write_text("echo mine\n")

    blobs.update_repos()
    assert (clone / "run.sh").read_text() == "echo mine\n"
    assert _stashes(clone) == ""


def test_store_inside_vault_is_refused(env):
    settings.update(blob_dir=str(env["vault"] / "Blobs"), public_url=PUBLIC)
    try:
        assert settings.blob_dir_inside_vault and not blobs.enabled()
    finally:
        settings.update(blob_dir="", public_url="")


def test_offload_refuses_store_without_symlinks(store, env, monkeypatch):
    """Clones are re-cloned, so this only concerns loose files that get copied."""
    loose = env["vault"] / "Dumps" / "git" / "misc"
    loose.mkdir(parents=True)
    (loose / "notes.txt").write_text("x")
    os.symlink("notes.txt", loose / "link.txt")
    age(loose)
    monkeypatch.setattr(blobs, "_supports_symlinks", lambda d: False)
    with pytest.raises(RuntimeError, match="can't hold symlinks"):
        blobs.offload(apply=True)
    assert (loose / "notes.txt").exists() and not list(store.rglob("notes.txt"))


def test_offload_cli_dry_run(env, tmp_path, capsys):
    from saver.__main__ import main
    build_unstored_vault(env["vault"], tmp_path / "up")
    store = tmp_path / "cli-store"
    store.mkdir()
    try:
        assert main(["offload", "--blobs", str(store), "--public-url", PUBLIC]) == 0
    finally:
        settings.update(blob_dir="", public_url="")
    out = capsys.readouterr().out
    assert "would move out of the vault" in out and "Dry run only" in out
    assert not any(store.iterdir())


def test_copy_into_vault_converts_text_to_utf8(tmp_path):
    from saver.blobs import copy_into_vault

    text = "# 电子书\n1.4亿在线背后的故事——腾讯QQ IM后台架构的演化与启示.ppt\n腾讯大讲堂、高性能服务器架构与分布式系统设计、数据库优化与运维实践合集\n"
    src = tmp_path / "readme.md"
    src.write_bytes(text.encode("gbk"))
    copy_into_vault(src, tmp_path / "out.md")
    assert (tmp_path / "out.md").read_text(encoding="utf-8") == text
    # UTF-8 text and binaries are copied byte for byte
    utf8 = tmp_path / "ok.md"
    utf8.write_text("café ✓\n", encoding="utf-8")
    copy_into_vault(utf8, tmp_path / "ok-out.md")
    assert (tmp_path / "ok-out.md").read_bytes() == utf8.read_bytes()
    blob = tmp_path / "image.png"
    blob.write_bytes(bytes(range(256)))
    copy_into_vault(blob, tmp_path / "image-out.png")
    assert (tmp_path / "image-out.png").read_bytes() == blob.read_bytes()


def test_watch_page_links_the_original(client, store):
    from saver.db import get_db

    # YouTube: from the id in the name (channel videos share one folder)
    make_video_dir(store / "youtube" / "Chan" / "Talk [abc123def45]")
    page = client.get("/watch/youtube/Chan/Talk%20%5Babc123def45%5D/Talk%20%5Babc123def45%5D.mkv").text
    assert 'id="original" href="https://www.youtube.com/watch?v=abc123def45"' in page
    assert "Original (youtube.com)" in page
    assert '\\.)youtube\\.com$' in page  # JS keeps its regex escapes
    # media.ccc.de: from the registry entry for the talk's folder
    talk = store / "video" / "media.ccc.de" / "39c3" / "CSS Clicker [16260]"
    talk.mkdir(parents=True)
    (talk / "talk.mp4").write_bytes(b"\0" * 64)
    url = "https://media.ccc.de/v/39c3-css-clicker"
    get_db().complete_entry("url:media.ccc.de/v/39c3-css-clicker", url=url,
                            note="Dumps/video/media.ccc.de/39c3/CSS Clicker [16260]")
    page = client.get("/watch/video/media.ccc.de/39c3/CSS%20Clicker%20%5B16260%5D/talk.mp4").text
    assert f'id="original" href="{url}"' in page and "Original (media.ccc.de)" in page
    # nothing known: no link
    other = store / "video" / "elsewhere"
    other.mkdir(parents=True)
    (other / "clip.mp4").write_bytes(b"\0" * 64)
    assert 'id="original"' not in client.get("/watch/video/elsewhere/clip.mp4").text


def test_video_is_filed_by_its_transcript_not_just_its_description(store, env, monkeypatch):
    """Analysis runs after the download, on the transcript's start as well:
    a description can be about something else entirely."""
    monkeypatch.setattr(media, "fetch_youtube_metadata", lambda url: {
        "id": "jNQXAC9IVRw", "title": "Me at the zoo", "uploader": "jawed",
        "description": "Links about microplastics in the brain"})
    analyzed = []

    def fake_download(url, target_dir):
        video = make_video_dir(target_dir)
        (target_dir / "out.txt").write_text("Here we are, one of the elephants.\n")
        return video

    def fake_analyze(self, text, allow_thin=False, require_content=True):
        analyzed.append(text)
        return ["zoo"], "A short clip at the zoo.", "nature/zoo.md"

    monkeypatch.setattr(media, "download_youtube_video", fake_download)
    monkeypatch.setattr(Saver, "analyze", fake_analyze)
    Saver().save("https://www.youtube.com/watch?v=jNQXAC9IVRw")

    assert len(analyzed) == 1
    text = analyzed[0]
    assert "elephants" in text and "microplastics" in text
    assert text.index("Transcript") < text.index("Description")


def test_video_falls_back_to_metadata_when_the_download_fails(store, env, monkeypatch):
    monkeypatch.setattr(media, "fetch_youtube_metadata", lambda url: {
        "id": "abc123def45", "title": "Talk", "uploader": "Chan", "description": "about things"})
    analyzed = []

    def failing_download(url, target_dir):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(media, "download_youtube_video", failing_download)
    monkeypatch.setattr(Saver, "analyze",
                        lambda self, text, **kw: analyzed.append(text) or (["x"], "s", "misc/x.md"))
    Saver().save("https://www.youtube.com/watch?v=abc123def45")
    assert analyzed == ["Title: Talk\nChannel: Chan\n\nDescription:\nabout things"]


def test_rejected_video_download_is_removed(store, env, monkeypatch):
    """A video the LLM judges content-free leaves no download behind; nothing
    would ever link to it."""
    from saver.errors import ContentFreeError
    monkeypatch.setattr(media, "fetch_youtube_metadata", lambda url: {
        "id": "abc123def45", "title": "Talk", "uploader": "Chan", "description": ""})
    monkeypatch.setattr(media, "download_youtube_video", lambda url, target_dir: make_video_dir(target_dir))

    def reject(self, text, allow_thin=False, require_content=True):
        raise ContentFreeError("no real content")
    monkeypatch.setattr(Saver, "analyze", reject)

    with pytest.raises(ContentFreeError):
        Saver().save("https://www.youtube.com/watch?v=abc123def45")
    assert not (store / "youtube" / "Chan" / "Talk [abc123def45]").exists()


def test_rejected_resave_keeps_an_existing_download(store, env, monkeypatch):
    from saver.errors import ContentFreeError
    monkeypatch.setattr(media, "fetch_youtube_metadata", lambda url: {
        "id": "abc123def45", "title": "Talk", "uploader": "Chan", "description": ""})
    folder = store / "youtube" / "Chan" / "Talk [abc123def45]"
    make_video_dir(folder)  # saved before; a re-save is now rejected
    monkeypatch.setattr(media, "download_youtube_video", lambda url, target_dir: target_dir / "Talk [abc123def45].mkv")
    monkeypatch.setattr(Saver, "analyze", lambda self, text, **kw: (_ for _ in ()).throw(ContentFreeError("thin")))
    with pytest.raises(ContentFreeError):
        Saver().save("https://www.youtube.com/watch?v=abc123def45")
    assert (folder / "Talk [abc123def45].mkv").exists()


# ---------------------------------------------------------------------------
# usage statistic
# ---------------------------------------------------------------------------
def wait_for_count(timeout=5):
    deadline = time.time() + timeout
    while blobs._counting and time.time() < deadline:
        time.sleep(0.01)
    assert not blobs._counting


@pytest.fixture
def fresh_count(monkeypatch):
    monkeypatch.setattr(blobs, "_counting", False)
    monkeypatch.setattr(blobs, "_count_error", None)


def fill_store(store):
    make_video_dir(store / "youtube" / "chan" / "A [abc123def45]")
    make_video_dir(store / "youtube" / "chan" / "B [bcd123def45]", stem="B [bcd123def45]")
    repo = store / "git" / "github.com" / "owner" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "main.c").write_bytes(b"x" * 1000)
    (store / "stray.bin").write_bytes(b"y" * 10)
    (store / "youtube" / "link.mkv").symlink_to(store / "stray.bin")  # not followed, not counted


def test_first_usage_counts_the_store_once_in_the_background(store, env, fresh_count):
    fill_store(store)
    assert blobs.usage()["counting"] is True  # nothing to show until the first count is done
    wait_for_count()
    u = blobs.usage()
    yt = u["categories"]["youtube"]
    assert yt["items"] == 2 and yt["bytes"] > 2 * 256 * 1024
    assert u["categories"]["git"] == {"bytes": 1000 + len("ref: refs/heads/main\n"), "items": 1}
    assert u["categories"]["video"] == {"bytes": 0, "items": 0}
    assert u["other_bytes"] == 10 and u["counting"] is False and u["counted_at"]
    assert u["bytes"] == yt["bytes"] + u["categories"]["git"]["bytes"] + 10
    # one row per unit: the repo, the channel, the stray file
    rows = env["db"]._rows("SELECT category, unit FROM blob_usage ORDER BY category, unit")
    assert rows == [{"category": "git", "unit": "github.com/owner/repo"},
                    {"category": "other", "unit": "stray.bin"},
                    {"category": "youtube", "unit": "chan"}]


def test_after_the_first_count_only_writes_recount_and_only_their_folder(store, env, fresh_count, monkeypatch):
    fill_store(store)
    blobs.usage()
    wait_for_count()
    before = blobs.usage()

    # a restart: the totals come from the database, no walk
    monkeypatch.setattr(blobs, "_full_count", lambda: pytest.fail("walked the whole store"))
    assert blobs.usage()["bytes"] == before["bytes"]

    walked = []
    real_measure = blobs._measure
    monkeypatch.setattr(blobs, "_measure", lambda c, p: walked.append(p) or real_measure(c, p))
    make_video_dir(store / "youtube" / "other-chan" / "C [cde123def45]", stem="C [cde123def45]")
    blobs.record_write("youtube", "other-chan/C [cde123def45]")
    assert walked == [store / "youtube" / "other-chan"]
    after = blobs.usage()
    assert after["categories"]["youtube"]["items"] == 3
    assert after["bytes"] > before["bytes"] + 256 * 1024

    # a folder that's gone drops out
    import shutil
    shutil.rmtree(store / "git" / "github.com" / "owner" / "repo")
    blobs.record_write("git", "github.com/owner/repo")
    assert blobs.usage()["categories"]["git"] == {"bytes": 0, "items": 0}


def test_recount_drops_what_is_gone_and_keeps_newer_writes(store, env, fresh_count):
    fill_store(store)
    env["db"].set_blob_unit("youtube", "deleted-chan", 999, 9, time.time() - 3600)
    assert blobs.recount() is True
    wait_for_count()
    assert "deleted-chan" not in {r["unit"] for r in env["db"]._rows("SELECT unit FROM blob_usage")}
    assert blobs.usage()["categories"]["youtube"]["items"] == 2


def test_no_store_no_usage(env, fresh_count):
    settings.update(blob_dir="")
    assert blobs.usage() is None and blobs.recount() is False
    blobs.record_write("git", "a/b/c")  # no-op


def test_storage_endpoints(store, env, fresh_count, monkeypatch):
    from saver.app import create_app
    client = TestClient(create_app(run_lifecycle=False))
    monkeypatch.setattr(blobs, "usage", lambda: {"counting": False, "bytes": 42})
    assert client.get("/api/storage").json()["usage"] == {"counting": False, "bytes": 42}
    started = []
    monkeypatch.setattr(blobs, "recount", lambda: started.append(1) or True)
    assert client.post("/api/storage/recount").json()["started"] is True and started == [1]
