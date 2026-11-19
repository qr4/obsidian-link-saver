import os

import pytest

from saver.util import write_text_atomic


def test_write_text_atomic_replaces_content_and_keeps_mode(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("old\n", encoding="utf-8")
    os.chmod(note, 0o640)
    write_text_atomic(note, "new\n")
    assert note.read_text(encoding="utf-8") == "new\n"
    assert note.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]


def test_write_text_atomic_keeps_the_old_file_when_writing_fails(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("old\n", encoding="utf-8")
    with pytest.raises(TypeError):
        write_text_atomic(note, None)  # fails mid-write
    assert note.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]
