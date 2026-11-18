import pytest

from saver.media import _new_media_file


def test_new_file_wins(tmp_path):
    (tmp_path / "Old [aaaaaaaaaaa].mkv").write_bytes(b"x")
    before = {p.name for p in tmp_path.iterdir()}
    (tmp_path / "New [bbbbbbbbbbb].mkv").write_bytes(b"x")
    (tmp_path / "New [bbbbbbbbbbb].en.srt").write_text("subs")
    assert _new_media_file(tmp_path, before).name == "New [bbbbbbbbbbb].mkv"


def test_already_downloaded_is_reused(tmp_path):
    """A force re-save: yt-dlp skips the download and creates nothing."""
    (tmp_path / "Other [aaaaaaaaaaa].mkv").write_bytes(b"x")
    (tmp_path / "Wanted [bbbbbbbbbbb].mkv").write_bytes(b"x")
    before = {p.name for p in tmp_path.iterdir()}
    assert _new_media_file(tmp_path, before, "bbbbbbbbbbb").name == "Wanted [bbbbbbbbbbb].mkv"
    with pytest.raises(RuntimeError):
        _new_media_file(tmp_path, before, "ccccccccccc")


def test_remove_partial_downloads(tmp_path):
    from saver.media import remove_partial_downloads
    video_dir = tmp_path / "Talk [abc]"
    video_dir.mkdir()
    (video_dir / "Talk [abc].f401.mp4.part").write_bytes(b"x")
    (video_dir / "Talk [abc].f401.mp4.ytdl").write_bytes(b"x")
    remove_partial_downloads(video_dir)
    assert not video_dir.exists()
    channel = tmp_path / "Channel"
    channel.mkdir()
    (channel / "Done [x].mkv").write_bytes(b"x")
    (channel / "New [y].f1.webm.part").write_bytes(b"x")
    remove_partial_downloads(channel)
    assert [p.name for p in channel.iterdir()] == ["Done [x].mkv"]
