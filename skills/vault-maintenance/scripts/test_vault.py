#!/usr/bin/env python3
"""Regression tests for the parsing primitives.

Every case here is a bug that actually shipped and returned a confident,
plausible, wrong answer - which is the failure mode worth guarding against:
none of them raised, they just quietly under-reported. Run `python3
test_vault.py` after touching vault.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vault import (WIKILINK_RE, Vault, extract_urls, is_content,
                   is_folder_note, norm_url, split_entries)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n     got:  {got!r}\n     want: {want!r}")


# ── wikilinks whose target contains brackets ──────────────────────────────
# Attachment names embed the video id: ".../Asahi Linux [16292].mkv". The old
# [^\]]+ body could never reach the closing ]] past an inner bracket, so these
# links matched nothing and were dropped from every link check.
check("bracketed attachment link",
      WIKILINK_RE.findall("\t[[Dumps/youtube/x/Asahi Linux [16292].mkv]]"),
      ["Dumps/youtube/x/Asahi Linux [16292].mkv"])
check("plain link", WIKILINK_RE.findall("[[note]]"), ["note"])
check("aliased link", WIKILINK_RE.findall("[[note|shown]]"), ["note|shown"])
check("two links on a line",
      WIKILINK_RE.findall("[[a]] and [[b]]"), ["a", "b"])

# ── clean_target: strip the alias and a heading anchor, keep a filename # ──
check("alias stripped", Vault.clean_target("Dumps/x/T [1].mkv|display"),
      "Dumps/x/T [1].mkv")
check("heading anchor stripped", Vault.clean_target("note#Section"), "note")
check("filename # kept",
      Vault.clean_target("Dumps/single_page/The Devils (The Devils, #1).html"),
      "Dumps/single_page/The Devils (The Devils, #1).html")

# ── URLs containing literal spaces ────────────────────────────────────────
# Two unrelated comic scans both truncated to ".../communism/Love" under a
# \S+ match and were reported as a duplicate URL.
a = "\thttps://lcamtuf.coredump.cx/communism/Love Letters 30: Love Thine Enemy"
b = "\thttps://lcamtuf.coredump.cx/communism/Love Secrets 32: I Fell For a Commie"
check("spaced URL kept whole", extract_urls(a),
      ["https://lcamtuf.coredump.cx/communism/Love Letters 30: Love Thine Enemy"])
check("spaced URLs stay distinct",
      norm_url(extract_urls(a)[0]) == norm_url(extract_urls(b)[0]), False)
check("inline URL still found",
      extract_urls("see https://example.com/x for more"),
      ["https://example.com/x"])

# ── is_content: hand-written notes and MOCs are not "empty" ───────────────
# flush() deletes notes with no content entries; index notes are headings and
# numbered lists with no `- ` bullets and once got deleted wholesale.
check("URL entry is content", is_content("- #tag\n\thttps://example.com\n"), True)
check("Dumps link is content", is_content("- #tag\n\t[[Dumps/x.html]]\n"), True)
check("waypoint line is not content", is_content("- **[[subfolder]]**\n"), False)
check("long hand-written prose is content",
      is_content("- " + "a note about the pipeline that links [[Tools/save-service]] " * 3),
      True)

# ── folder notes ──────────────────────────────────────────────────────────
check("folder note", is_folder_note("hobbies/hobbies.md"), True)
check("vault index", is_folder_note("Areas.md"), True)
check("leaf note", is_folder_note("hobbies/coffee.md"), False)

# ── entry splitting keeps continuation lines with their bullet ────────────
FLAG = "%" * 2 + " Waypoint " + "%" * 2  # assembled: the Waypoint plugin rewrites files containing it
pre, ents = split_entries(FLAG + "\n- #a\n\tsummary\n\thttps://x.test\n- #b\n\tsecond\n")
check("preamble kept", pre.strip(), FLAG)
check("two entries", len(ents), 2)
check("continuation lines attached", ents[0].count("\n"), 3)

# ── retag handles both entry shapes ───────────────────────────────────────
# Tagged entries lead with "- #a #b" and indent the summary; untagged ones put
# the summary straight on the bullet. retag() once refused the second shape,
# which is exactly what diag.py's "entries without tags" list contains.
import migrate  # noqa: E402

m = migrate.Migration.__new__(migrate.Migration)
m.notes = {
    "t.md": ["- #old\n\tSummary here.\n\thttps://x.test\n",
             "- Untagged summary.\n\thttps://y.test\n"],
}
m.preamble = {"t.md": ""}
m.deleted = set()
m.retag("t.md", "Summary here", ["a", "b"])
m.retag("t.md", "Untagged summary", ["c"])
check("retag replaces an existing tag line",
      m.notes["t.md"][0], "- #a #b\n\tSummary here.\n\thttps://x.test\n")
check("retag inserts a tag line when there is none",
      m.notes["t.md"][1], "- #c\n\tUntagged summary.\n\thttps://y.test\n")
# and the result must parse back as tagged, not as prose
check("retagged entry reparses",
      split_entries(m.notes["t.md"][1])[1][0].splitlines()[0], "- #c")

# ── flush() refuses to write outside the migration's lock window ──────────
# flush() rewrites every note wholesale from the model built when the notes
# were read. A plan that read outside the lock and wrote inside it would
# silently revert anything the save service appended in between - no error,
# no conflict, just a lost entry.
migrate.DRY = True  # belt and braces: nothing below may touch the disk

try:
    migrate.Migration().flush()
    check("flush outside the lock window raises", "no exception", "RuntimeError")
except RuntimeError as e:
    check("flush outside the lock window raises",
          "lock window" in str(e), True)


def lock_is_held():
    """True if some open file description holds the vault flock.

    A second fd conflicts even within this process - which is exactly why
    flush() verifies the window rather than acquiring the lock itself.
    """
    import fcntl
    from vault import LOCK
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


with migrate.Migration() as _m:
    check("lock is held while the notes are readable", lock_is_held(), True)
    check("entering reads the vault", len(_m.notes) > 0, True)
    check("flush is permitted inside the window", _m._lock is not None, True)
# not asserting the lock is free here: another tool may legitimately hold
# it the instant we let go. The handle being dropped is the deterministic part.
check("window closed on exit", _m._lock, None)

# ── flush() writes only changed notes, atomically ──────────────────────────
# It used to rewrite all ~1500 notes in place with write_text: a stop mid-way
# left a cut-off note for LiveSync to spread, and every timestamp changed.
import os as _os
import tempfile as _tempfile
from vault import write_if_changed

with _tempfile.TemporaryDirectory() as _tmp:
    _note = Path(_tmp) / "note.md"
    _note.write_text("- a\n", encoding="utf-8")
    _os.utime(_note, (1, 1))
    check("unchanged note is not written", write_if_changed(_note, "- a\n"), False)
    check("unchanged note keeps its timestamp", _note.stat().st_mtime, 1)
    check("changed note is written", write_if_changed(_note, "- b\n"), True)
    check("changed note has the new text", _note.read_text(encoding="utf-8"), "- b\n")
    check("no temp file left behind", sorted(p.name for p in Path(_tmp).iterdir()), ["note.md"])
    check("a new note is created", write_if_changed(Path(_tmp) / "new.md", "- c\n"), True)

if failures:
    print(f"FAILED {len(failures)}:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all parsing regression tests pass")
