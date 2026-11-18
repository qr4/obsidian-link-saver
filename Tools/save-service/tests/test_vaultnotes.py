from saver import vaultnotes
from saver.vaultnotes import replace_area_entry, validate_area_path


def test_waypoint_flag_never_written_literally():
    """The Waypoint plugin rewrites any vault file containing its trigger,
    so the package sources must only ever assemble it at runtime."""
    from pathlib import Path
    flag = vaultnotes._WAYPOINT_FLAG_PART_A + vaultnotes._WAYPOINT_FLAG_PART_B
    root = Path(vaultnotes.__file__).resolve().parents[1]
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".js", ".html", ".css", ".md", ".toml", ".yml", ".xml", ".sh"):
            assert flag not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_waypoints_for_nested_leaf(env):
    vaultnotes.materialize_area("graphics/raymarching/sdf.md")
    areas = env["vault"] / "Areas"
    assert (areas / "graphics" / "raymarching" / "sdf.md").exists()
    for folder_note in (areas / "graphics" / "graphics.md", areas / "graphics" / "raymarching" / "raymarching.md"):
        assert folder_note.read_text() == vaultnotes.WAYPOINT_FLAG + "\n"


def test_replace_area_entry_moves_bullet(env):
    note, old = replace_area_entry("a/one.md", ["x"], "First.", "https://u")
    note2, new = replace_area_entry("b/two.md", ["y"], "Second.", "https://u", note, old)
    assert (env["vault"] / "Areas" / "a" / "one.md").read_text() == ""
    assert (env["vault"] / "Areas" / "b" / "two.md").read_text() == new
    assert new == "- #y\n\tSecond.\n\thttps://u\n"


def test_validate_area_path(env):
    assert validate_area_path("programming/rust/errors.md") == "programming/rust/errors.md"
    assert validate_area_path("../outside.md") == ""
    assert validate_area_path("hobbies/board-games/zones.png") == ""
    assert validate_area_path("productivity/productivity.md") == ""
