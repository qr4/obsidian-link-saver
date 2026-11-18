from saver import waypoints
from saver.config import settings
from saver.waypoints import BEGIN_WAYPOINT, KINDS, LANDMARK_FLAG, WAYPOINT_FLAG, apply_block, refresh

END = KINDS["waypoint"][1]


def make(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def build(tmp_path):
    vault = tmp_path / "vault"
    areas = vault / "Areas"
    make(areas / "Areas.md", WAYPOINT_FLAG + "\n")
    make(areas / "topic" / "topic.md", "intro text\n\n" + WAYPOINT_FLAG + "\n\nfooter\n")
    make(areas / "topic" / "Zebra.md")
    make(areas / "topic" / "alpha.md")
    make(areas / "topic" / "item10.md")
    make(areas / "topic" / "item2.md")
    make(areas / "topic" / "diagram.png")
    make(areas / "topic" / "notes.sync-conflict-20260908-133522-CBWFGIL.md")
    make(areas / "topic" / "_attachments" / "a.md")
    make(areas / "topic" / "sub" / "sub.md", WAYPOINT_FLAG + "\n")     # stops the scan
    make(areas / "topic" / "sub" / "deep.md")
    make(areas / "topic" / "loose" / "inner.md")                     # no folder note: inlined
    make(areas / "topic" / "loose" / "nested" / "x.md")
    make(areas / "topic" / "mark" / "mark.md", LANDMARK_FLAG + "\n")  # landmarks don't stop it
    make(areas / "topic" / "mark" / "m1.md")
    make(areas / "topic" / "plain" / "plain.md", "just a note\n")   # folder note without waypoint
    make(areas / "topic" / "plain" / "p1.md")
    make(vault / "Dumps" / "git" / "x" / "x.md", WAYPOINT_FLAG + "\n")  # never scanned
    return vault


def test_renders_like_the_plugin(tmp_path):
    vault = build(tmp_path)
    tree = waypoints.Renderer(vault).tree(vault / "Areas" / "topic")
    assert tree == "\n".join([
        "- [[alpha]]",
        "- [[item2]]",
        "- [[item10]]",
        "- **loose**",
        "\t- [[inner]]",
        "\t- **nested**",
        "\t\t- [[x]]",
        "- **[[mark]]**",
        "\t- [[m1]]",
        "- **[[plain]]**",
        "\t- [[p1]]",
        "- **[[sub]]**",
        "- [[Zebra]]",
    ])


def test_refresh_fills_flags_and_keeps_surrounding_text(tmp_path):
    vault = build(tmp_path)
    changed = refresh(vault)
    topic = (vault / "Areas" / "topic" / "topic.md").read_text()
    assert topic.startswith("intro text\n\n" + BEGIN_WAYPOINT + "\n- [[alpha]]")
    assert topic.endswith("- [[Zebra]]\n\n" + END + "\n\nfooter\n")
    assert (vault / "Areas" / "Areas.md").read_text() == f"{BEGIN_WAYPOINT}\n- **[[topic]]**\n\n{END}\n"
    assert (vault / "Areas" / "topic" / "mark" / "mark.md").read_text().startswith(KINDS["landmark"][0])
    assert vault / "Dumps" / "git" / "x" / "x.md" not in changed
    assert refresh(vault) == []  # idempotent: nothing rewritten


def test_existing_block_is_replaced_in_place():
    old = f"before\n{BEGIN_WAYPOINT}\n- [[gone]]\n\n{END}\nafter"
    assert apply_block(old, "- [[new]]", "waypoint") == f"before\n{BEGIN_WAYPOINT}\n- [[new]]\n\n{END}\nafter"
    assert apply_block("no block here", "- x", "waypoint") is None


def test_callout_waypoint():
    out = apply_block(f"> {WAYPOINT_FLAG}", "- [[a]]", "waypoint")
    assert out == f">[!waypoint]\n>{BEGIN_WAYPOINT}\n>- [[a]]\n>\n>{END}"
    again = apply_block(out, "- [[b]]", "waypoint")
    assert again == f">[!waypoint]\n>{BEGIN_WAYPOINT}\n>- [[b]]\n>\n>{END}"  # header kept, not duplicated


def test_signature_tracks_structure_not_content(tmp_path):
    vault = build(tmp_path)
    refresh(vault)
    before = waypoints.structure_signature(vault)
    (vault / "Areas" / "topic" / "alpha.md").write_text("edited body")
    assert waypoints.structure_signature(vault) == before
    make(vault / "Areas" / "topic" / "beta.md")
    assert waypoints.structure_signature(vault) != before


def test_plugin_detection(tmp_path):
    vault = tmp_path / "v"
    make(vault / ".obsidian" / "community-plugins.json", '["dataview", "waypoint"]')
    assert waypoints.plugin_enabled(vault)
    make(vault / ".obsidian" / "community-plugins.json", '["dataview"]')
    assert not waypoints.plugin_enabled(vault)


def test_save_updates_indexes_immediately(env, monkeypatch):
    from saver import pipeline
    from saver.pipeline import Saver

    make(env["vault"] / "Areas" / "programming" / "programming.md", WAYPOINT_FLAG + "\n")

    def fake_single_file(url, dest_dir, filename):
        dest_dir.mkdir(parents=True, exist_ok=True)
        return make(dest_dir / filename, "<html><title>T</title><body>Some content</body></html>")

    monkeypatch.setattr(pipeline, "save_single_file", fake_single_file)
    monkeypatch.setattr(pipeline, "fetch_page_html", lambda url: "<html></html>")
    monkeypatch.setattr(pipeline, "is_pdf_url", lambda url: False)
    settings.update(manage_waypoints=True)
    try:
        Saver().save("https://blog.example.com/scraping")
    finally:
        settings.update(manage_waypoints=False)
    index = (env["vault"] / "Areas" / "programming" / "programming.md").read_text()
    assert "- [[web-scraping]]" in index and "- [[python]]" in index
