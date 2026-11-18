"""Offload over SSH, exercised with a "local" host: the same scripts run
through bash and rsync targets a local path, so only the SSH hop is faked."""
import os

import pytest

from saver.config import settings
from saver.remote_offload import SshStore, offload_ssh
from test_blobs import PUBLIC, build_unstored_vault, git


@pytest.fixture
def remote(env, tmp_path):
    settings.update(public_url=PUBLIC, offload_min_age=600)
    store = tmp_path / "server" / "obsidian_blobs"
    yield f"local:{store}", store
    settings.update(public_url="")


def test_dry_run_touches_nothing(env, remote, tmp_path):
    spec, store = remote
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    before = note.read_text()
    plans = offload_ssh(spec, apply=False)
    assert [p.name for p in plans["youtube"]["bulk"]] == ["Talk [abc123def45].mkv"]
    assert video.exists() and (repo / ".git").is_dir() and note.read_text() == before
    assert not (store / "git").exists() and not (store / "youtube").exists()


def test_apply_moves_videos_and_reclones_on_the_server(env, remote, tmp_path):
    spec, store = remote
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    video_bytes = video.read_bytes()
    offload_ssh(spec, apply=True, jobs=2)

    rel = "Chan/Talk [abc123def45]"
    assert (store / "youtube" / rel / video.name).read_bytes() == video_bytes and not video.exists()
    assert (store / "youtube" / rel / "out.srt").exists()  # transcripts copied along
    vault_dir = env["vault"] / "Dumps" / "youtube" / rel
    watch = (vault_dir / ".offloaded").read_text().strip()
    assert f"[▶ Watch]({watch}) · [[Dumps/youtube/{rel}/out.txt|Transcript]]" in note.read_text()
    assert fresh.exists()

    clone = store / "git" / "github.com" / "o" / "r"
    assert (clone / ".git" / "HEAD").exists() and os.readlink(clone / "LICENSE") == "COPYING"
    assert sorted(p.name for p in repo.iterdir()) == [".offloaded", "README.md"]
    assert (repo / ".offloaded").read_text().strip() == f"{PUBLIC}/blobs/git/github.com/o/r/"
    assert sorted(p.name for p in gist.iterdir()) == [".offloaded", "notes.md"]
    assert not list(store.rglob("*.cloning"))

    plans = offload_ssh(spec, apply=False)  # nothing left
    assert not any(p["bulk"] for p in plans.values()) and not plans["git"]["repos"]


def test_conflicting_server_file_is_left_in_vault(env, remote, tmp_path):
    spec, store = remote
    video, *_ = build_unstored_vault(env["vault"], tmp_path / "up")
    clash = store / "youtube" / "Chan" / "Talk [abc123def45]" / video.name
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"different")
    offload_ssh(spec, apply=True)
    assert video.exists() and clash.read_bytes() == b"different"


def test_failed_server_clone_leaves_repo(env, remote, tmp_path):
    spec, store = remote
    video, repo, gist, fresh, note = build_unstored_vault(env["vault"], tmp_path / "up")
    git("remote", "set-url", "origin", (tmp_path / "gone").as_uri(), cwd=repo)
    old = os.stat(repo / "README.md").st_mtime
    os.utime(repo / ".git" / "config", (old, old))
    offload_ssh(spec, apply=True)
    assert (repo / ".git" / "HEAD").exists() and (repo / "src" / "main.c").exists()
    assert not (store / "git" / "github.com" / "o" / "r").exists()


def test_problems_reported(tmp_path):
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        assert any("not writable" in p for p in SshStore(f"local:{readonly}/x").problems())
    finally:
        readonly.chmod(0o700)
    assert SshStore(f"local:{tmp_path / 'ok'}").problems() == []


def test_spec_parsing():
    store = SshStore("user@server.example.com:/srv/obsidian-blobs/")
    assert (store.host, store.base) == ("user@server.example.com", "/srv/obsidian-blobs")
    assert store.path("git", "github.com/a/b") == "/srv/obsidian-blobs/git/github.com/a/b"
    with pytest.raises(ValueError):
        SshStore("/no/host")
