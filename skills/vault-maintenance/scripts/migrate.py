#!/usr/bin/env python3
"""Entry-level migration primitives for the Areas/ restructure.

Operations address *entries* (one `- ` bullet plus its indented lines) and
notes by path, never line numbers, so a plan stays valid even if the save
service appended to a file since the plan was written.

Migration is a context manager and must be used as one - it holds the vault
lock across the whole read-mutate-write cycle. See its docstring for why
locking only the write is not enough.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vault import AREAS, split_entries, is_content, is_folder_note, file_lock, write_if_changed

DRY = True
log = []


class Migration:
    """A migration owns one lock window: it is a context manager.

        with Migration() as m:
            m.move_many(...)
        # flush() runs on the way out, still holding the lock

    The lock is taken *before* the notes are read and held until flush() has
    written them back. That span is the whole point. flush() does not patch
    files, it rewrites every note wholesale from the in-memory model, so an
    append by the save service between the read and the write
    would be silently reverted - no error, no conflict, just a lost entry.
    Locking only the write would not close that window, because the window
    opens when the notes are read.

    flush() cannot take the lock itself: vault.file_lock() flocks a fresh fd
    per call, so a nested acquire in this process would block on itself.
    It therefore verifies the window is open and refuses otherwise.
    """

    def __init__(self):
        self.notes, self.preamble = {}, {}
        self.deleted = set()
        self._lock = None

    def __enter__(self):
        self._lock = file_lock()
        self._lock.__enter__()
        for p in sorted(AREAS.rglob("*.md")):
            rel = str(p.relative_to(AREAS))
            pre, ents = split_entries(p.read_text(encoding="utf-8"))
            self.notes[rel], self.preamble[rel] = ents, pre
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.flush()
        finally:
            lock, self._lock = self._lock, None
            lock.__exit__(exc_type, exc, tb)
        return False

    def content(self, rel):
        return [e for e in self.notes.get(rel, []) if is_content(e)]

    def find(self, rel, needle):
        hits = [i for i, e in enumerate(self.notes.get(rel, [])) if needle in e]
        if len(hits) != 1:
            raise LookupError(f"{rel}: {len(hits)} matches for {needle!r}")
        return hits[0]

    # ---- note-level
    def move(self, src, dst):
        if src not in self.notes:
            raise LookupError(f"move: missing {src}")
        if src == dst:
            return
        if dst in self.notes and self.content(dst):
            raise LookupError(f"move: {dst} exists with content")
        self.notes[dst] = self.notes.pop(src)
        self.preamble[dst] = self.preamble.pop(src, "")
        self.deleted.add(src)
        log.append(f"MOVE   {src} -> {dst}")

    def move_many(self, srcdir, names, dstdir):
        for n in names:
            self.move(f"{srcdir}/{n}.md", f"{dstdir}/{n}.md")

    def merge(self, src, dst):
        if src not in self.notes:
            raise LookupError(f"merge: missing {src}")
        moved = self.content(src)
        self.notes.setdefault(dst, [])
        self.preamble.setdefault(dst, "")
        self.notes[dst].extend(moved)
        self.notes.pop(src)
        self.preamble.pop(src, None)
        self.deleted.add(src)
        log.append(f"MERGE  {src} -> {dst} ({len(moved)})")

    def delete(self, rel, why=""):
        if self.content(rel):
            raise LookupError(f"delete: {rel} still has content")
        self.notes.pop(rel, None)
        self.preamble.pop(rel, None)
        self.deleted.add(rel)
        log.append(f"DELETE {rel} {why}")

    # ---- entry-level
    def move_entry(self, src, needle, dst):
        i = self.find(src, needle)
        e = self.notes[src].pop(i)
        self.notes.setdefault(dst, [])
        self.preamble.setdefault(dst, "")
        self.notes[dst].append(e)
        log.append(f"ENTRY  {src} -> {dst} [{needle[:45]}]")

    def drop_entry(self, rel, needle, why=""):
        i = self.find(rel, needle)
        self.notes[rel].pop(i)
        log.append(f"DROP   {rel} [{needle[:45]}] {why}")

    def edit(self, rel, needle, old, new):
        i = self.find(rel, needle)
        e = self.notes[rel][i]
        if old not in e:
            raise LookupError(f"edit: {old!r} not in {rel} entry")
        self.notes[rel][i] = e.replace(old, new, 1)
        log.append(f"EDIT   {rel}: {old[:45]!r} -> {new[:45]!r}")

    def retag(self, rel, needle, newtags):
        """Set the tag line of one entry, adding one if it has none.

        The service writes a tagged entry as "- #a #b" with the summary
        indented beneath, and an untagged one as "- <summary>" with no tag
        line at all. Refusing the second shape would miss exactly what
        diag.py's "entries without tags" list is made of, so the primitive
        couldn't fix the problem it was meant for. Promoting the summary to
        an indented line and inserting the tag line above it converts one
        shape into the other."""
        i = self.find(rel, needle)
        lines = self.notes[rel][i].splitlines(keepends=True)
        tag_line = "- " + " ".join("#" + t for t in newtags) + "\n"
        if lines[0].startswith("- #"):
            lines[0] = tag_line
        else:
            summary = lines[0][2:].lstrip()          # drop the "- " bullet
            lines[0:1] = [tag_line, "\t" + summary]
        self.notes[rel][i] = "".join(lines)
        log.append(f"RETAG  {rel}: {newtags}")

    # ---- persistence
    def has_prose(self, rel):
        """True if the note carries hand-written content outside the bullet
        convention - the curriculum/series MOCs are headings and numbered
        lists, so content() sees nothing and the emptied-note sweep below
        would delete them outright. It did, once."""
        return bool(self.preamble.get(rel, "").strip())

    def flush(self):
        if self._lock is None:
            raise RuntimeError(
                "flush() outside the migration's lock window - write the plan "
                "as `with Migration() as m:` instead. flush() rewrites every "
                "note from the in-memory model, so anything the save service "
                "appended since the notes were read would be silently "
                "discarded.")
        for rel in [r for r in list(self.notes)
                    if not is_folder_note(r) and not self.content(r)
                    and not self.has_prose(r)]:
            self.notes.pop(rel)
            self.preamble.pop(rel, None)
            self.deleted.add(rel)
            log.append(f"DELETE {rel} (emptied by migration)")
        if DRY:
            return
        for rel in sorted(self.deleted):
            p = AREAS / rel
            if p.exists() and rel not in self.notes:
                p.unlink()
        for rel, ents in sorted(self.notes.items()):
            p = AREAS / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(ents)
            if body and not body.endswith("\n"):
                body += "\n"
            write_if_changed(p, self.preamble.get(rel, "") + body)
        for d in sorted(AREAS.rglob("*"), key=lambda x: -len(x.parts)):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                log.append(f"RMDIR  {d.relative_to(AREAS)}")
