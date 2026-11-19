"""Writes into the vault's notes: Area leaf bullets, Waypoint folder notes,
Graph View color groups, plus the tag census fed back to the tagger.
file_lock() is the service-wide vault lock."""
import colorsys
import json
import os
import re
from pathlib import Path

from .config import settings
from .jobctx import log
from .util import create_path, write_text_atomic
from .vaultlock import vault_lock

# The Obsidian Waypoint plugin watches every vault file - not just markdown -
# for its trigger text, and this source tree lives inside the vault. Writing
# the trigger out in one piece anywhere in this package (even in a comment)
# makes the plugin generate a waypoint into the .py file itself and overwrite
# it, so the two halves are only ever joined at runtime.
_WAYPOINT_FLAG_PART_A = "%%"
_WAYPOINT_FLAG_PART_B = " Waypoint %%"
WAYPOINT_FLAG = _WAYPOINT_FLAG_PART_A + _WAYPOINT_FLAG_PART_B

UNCATEGORIZED_AREA = "uncategorized.md"


def _generate_group_color(index: int) -> int:
    """Deterministic, well-spread color for the index'th Graph View color
    group, via golden-angle hue rotation. Returns Obsidian's expected
    0xRRGGBB packed as a decimal int."""
    hue = (index * 0.618033988749895) % 1.0  # golden ratio conjugate, in turns
    r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.65)
    return (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)


def ensure_graph_color_group(area_folder: str):
    """Give a top-level Area folder its own Graph View color group if it
    doesn't already have one. Additive only - never touches existing groups
    or any other graph.json setting, so manual tweaks made in Obsidian
    survive."""
    query = f'path:"Areas/{area_folder}"'
    path = settings.graph_config_path
    with vault_lock():
        try:
            config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError:
            config = {}
        color_groups = config.setdefault("colorGroups", [])
        if any(g.get("query") == query for g in color_groups):
            return
        color_groups.append({"query": query, "color": {"a": 1, "rgb": _generate_group_color(len(color_groups))}})
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(config, indent=2))


def ensure_waypoints(area_path: str):
    """For every ancestor folder of a newly created Area note, drop a folder
    note containing the Waypoint plugin's flag if one doesn't already exist
    (folder-note style: Areas/homelab/homelab.md). The plugin then keeps the
    actual link list current while Obsidian runs; this only seeds the flag.

    Waypoint's scan stops recursing at another Waypoint note, so giving every
    level its own keeps each folder note showing only its direct children.

    Also gives the leaf's top-level Area a Graph View color group - one color
    per top-level Area, not per nested subfolder."""
    parts = Path(area_path).parts
    dirs = parts[:-1]
    if not dirs:
        return  # leaf sits directly in Areas/ - no folder to color or waypoint
    ensure_graph_color_group(dirs[0])
    if len(dirs) < 2:
        return

    for depth in range(len(dirs)):
        dir_name = dirs[depth]
        waypoint_path = Path(settings.areas_dir, *dirs[: depth + 1], f"{dir_name}.md")
        # Atomic create-if-missing (O_EXCL) so two saves landing under the
        # same new folder don't both write it.
        try:
            fd = os.open(waypoint_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{WAYPOINT_FLAG}\n")


TAG_BULLET_RE = re.compile(r"^- (#[a-z0-9][\w-]*(?:\s+#[a-z0-9][\w-]*)*)\s*$")

# How many times one leaf note may vote for the same tag in the census.
PER_NOTE_TAG_CAP = 3


def collect_existing_tags() -> list:
    """Every tag already used across Areas/ leaf notes, most-used first.
    Fed back to the tagger so it reuses an existing spelling instead of
    minting a near-duplicate synonym (raymarching vs ray-marching, csharp vs
    c-sharp both happened before this existed).

    A single leaf only contributes PER_NOTE_TAG_CAP counts per tag: one bulk
    crawl (357 theoremoftheday.org saves in one leaf) otherwise dictates the
    whole vocabulary handed to the tagger."""
    counts = {}
    for path in Path(settings.areas_dir).rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        per_note = {}
        for line in text.splitlines():
            match = TAG_BULLET_RE.match(line)
            if not match:
                continue
            for tag in re.findall(r"#([a-z0-9][\w-]*)", match.group(1)):
                per_note[tag] = per_note.get(tag, 0) + 1
        for tag, n in per_note.items():
            counts[tag] = counts.get(tag, 0) + min(n, PER_NOTE_TAG_CAP)
    return [tag for tag, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def validate_area_path(area: str) -> str:
    """Reject an LLM-proposed leaf path that would escape Areas/, isn't a
    .md file, or collides with the folder-note convention. Returns "" (the
    uncategorized fallback) on rejection."""
    from .util import is_safe_relative_path

    if not area:
        return ""
    if not is_safe_relative_path(settings.areas_dir, area):
        log(f"WARNING: model returned unsafe area path {area!r}, ignoring")
        return ""
    if not area.endswith(".md"):
        # e.g. "hobbies/board-games/zones.png" - the model echoed an image's
        # own extension back instead of picking a .md leaf name.
        log(f"WARNING: model returned a non-.md area path {area!r}, falling back to uncategorized")
        return ""
    if Path(area).stem == Path(area).parent.name:
        # e.g. "productivity/productivity.md" - a note named after its own
        # folder is the Waypoint plugin's auto-managed index, which silently
        # overwrites whatever real content lands there.
        log(f"WARNING: model returned a leaf matching its own folder name {area!r} "
            "(collides with the folder-note convention), falling back to uncategorized")
        return ""
    return area


def materialize_area(area: str):
    """Create the leaf note file and its Waypoint/graph scaffolding."""
    if area:
        create_path(settings.areas_dir / area)
        ensure_waypoints(area)


def format_area_bullet(tags, summary: str, source_line: str) -> str:
    """One Area-note bullet. Tags lead as the bullet's own "- " line so
    skimming a leaf shows what each entry is about; the summary and link(s)
    follow as indented continuation lines."""
    if tags:
        lines = ["- " + " ".join(f"#{t}" for t in tags), f"\t{summary}"]
    else:
        lines = [f"- {summary}"]
    for ref_line in source_line.split("\n"):
        lines.append(f"\t{ref_line}")
    return "\n".join(lines) + "\n"


def append_area_entry(area: str, tags, summary: str, source_line: str) -> tuple:
    """Record a saved item as one bullet in its Area leaf note. Falls back to
    Areas/uncategorized.md when area resolution failed. Returns (area_note,
    content) so a later --force run can find and replace this bullet."""
    area_note = area or UNCATEGORIZED_AREA
    area_path = settings.areas_dir / area_note
    content = format_area_bullet(tags, summary, source_line)
    with vault_lock():
        area_path.parent.mkdir(parents=True, exist_ok=True)
        with open(area_path, "a", encoding="utf-8") as f:
            f.write(content)
    return area_note, content


def replace_area_entry(area: str, tags, summary: str, source_line: str,
                       old_area_note: str = None, old_content: str = None) -> tuple:
    """append_area_entry(), but removes a prior bullet first when
    old_area_note/old_content are given - both steps in one critical
    section, so a concurrent save on the same file can't interleave between
    them and leave duplicate bullets behind (which happened when they were
    two separate lock acquisitions)."""
    area_note = area or UNCATEGORIZED_AREA
    area_path = settings.areas_dir / area_note
    content = format_area_bullet(tags, summary, source_line)

    removed = False
    with vault_lock():
        if old_area_note and old_content:
            old_path = settings.areas_dir / old_area_note
            if old_path.exists():
                old_text = old_path.read_text(encoding="utf-8")
                if old_content in old_text:
                    write_text_atomic(old_path, old_text.replace(old_content, "", 1))
                    removed = True
        area_path.parent.mkdir(parents=True, exist_ok=True)
        with open(area_path, "a", encoding="utf-8") as f:
            f.write(content)

    if old_area_note and old_content and not removed:
        log(f"WARNING: could not find the previous entry in Areas/{old_area_note} to replace "
            "(hand-edited since, or moved?) - appending a new one alongside it")
    return area_note, content
