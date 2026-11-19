"""Offload over SSH: run the move from the machine holding the vault, but
do the heavy lifting on the server.

Writing a git checkout through an SMB mount means one network round trip
per file - about 40 files/s, i.e. hours for the ~500k files in Dumps/git.
Over SSH instead:

  - git clones run on the server itself (its disk, its internet link),
    several at a time, then are handed to nobody:users for the container
  - video files go over with rsync and are verified with
    `rsync --checksum --dry-run` (both ends hash their own copy)
  - notes, markers and deletions stay local, and nothing is deleted from
    the vault before its server copy checks out

One multiplexed SSH connection is opened at the start (ControlMaster), so
a password - if there is no key - is asked for once.

    python -m saver offload --ssh user@server:/path/to/obsidian-blobs \\
        --vault ~/obsidian --public-url https://saver.example.com [--apply]
"""
import os
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import blobs, proc
from .config import settings
from .jobctx import check_cancelled, log, progress, result, stage
from .vaultlock import vault_lock

q = shlex.quote


class SshStore:
    """The blob store on a remote host, reached over one SSH connection.
    A host of "local" runs the same commands on this machine (tests)."""

    def __init__(self, spec: str):
        if ":" not in spec:
            raise ValueError(f"expected user@host:/path, got {spec!r}")
        self.host, base = spec.split(":", 1)
        self.base = base.rstrip("/") or "/"
        self.local = self.host == "local"
        self.control = os.path.join(tempfile.gettempdir(), f"saver-ssh-{os.getpid()}.sock")

    # ---- connection -----------------------------------------------------
    def connect(self):
        if self.local:
            return
        # Interactive on purpose: asks for a password here if there is no key.
        subprocess.run(["ssh", "-o", "ControlMaster=yes", "-o", f"ControlPath={self.control}",
                        "-o", "ControlPersist=900", "-o", "ConnectTimeout=15", "-fN", self.host], check=True)

    def close(self):
        if not self.local:
            subprocess.run(["ssh", "-o", f"ControlPath={self.control}", "-O", "exit", self.host],
                           capture_output=True)

    def _ssh(self) -> list:
        return ["ssh", "-o", f"ControlPath={self.control}", "-o", "BatchMode=yes", self.host]

    def run(self, script: str, timeout=None):
        cmd = ["bash", "-c", script] if self.local else [*self._ssh(), script]
        return proc.run(cmd, timeout=timeout)

    def path(self, *parts) -> str:
        return "/".join([self.base, *parts])

    # ---- checks ----------------------------------------------------------
    def problems(self) -> list:
        script = (f"for t in git rsync; do command -v $t >/dev/null || echo \"missing: $t\"; done; "
                  f"mkdir -p {q(self.base)} && test -w {q(self.base)} || echo \"not writable: {self.base}\"")
        r = self.run(script, timeout=60)
        out = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()]
        return out if (out or r.returncode == 0) else [f"ssh failed (exit {r.returncode})"]

    def existing_clones(self, roots) -> set:
        roots = list(roots)
        if not roots:
            return set()
        script = (f"cd {q(self.path('git'))} 2>/dev/null || exit 0; for d in {' '.join(q(r) for r in roots)}; "
                  "do [ -f \"$d/.git/HEAD\" ] && printf '%s\\n' \"$d\"; done; true")
        return set(self.run(script, timeout=300).stdout.split("\n")) - {""}

    # ---- git ---------------------------------------------------------------
    def reclone(self, url: str, root: str):
        """Shallow-clone on the server into a temporary sibling, then rename,
        so the destination is a complete clone or absent. (ok, error)."""
        dest = self.path("git", root)
        parent, name = dest.rsplit("/", 1)
        tmp = f"{parent}/.{name}.cloning"
        script = (
            f"export GIT_TERMINAL_PROMPT=0; rm -rf {q(tmp)}; mkdir -p {q(parent)} || exit 1; "
            f"git clone --depth 1 --quiet {q(url)} {q(tmp)} 2>&1 || {{ rm -rf {q(tmp)}; exit 3; }}; "
            f"test -f {q(tmp)}/.git/HEAD || {{ rm -rf {q(tmp)}; echo 'clone incomplete'; exit 4; }}; "
            f"rm -rf {q(dest)} && mv {q(tmp)} {q(dest)} || exit 5; "
            f"chown -R 99:100 {q(dest)} 2>/dev/null; true")
        r = self.run(script, timeout=3600)
        lines = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()]
        return r.returncode == 0, (lines[-1] if lines else f"exit {r.returncode}")

    # ---- files ---------------------------------------------------------------
    def _rsync(self, extra: list, source_dir: Path, category: str, rels, capture: bool):
        target = self.path(category) + "/"
        cmd = ["rsync", "-s", "--from0", "--files-from=-", *extra]
        if not self.local:
            cmd += ["-e", " ".join(self._ssh()[:-1]), f"{source_dir}/", f"{self.host}:{target}"]
        else:
            os.makedirs(target, exist_ok=True)
            cmd += [f"{source_dir}/", target]
        listing = ("\0".join(str(r) for r in rels) + "\0").encode()
        return subprocess.run(cmd, input=listing, capture_output=capture)

    def copy(self, source_dir: Path, category: str, rels) -> int:
        """Copy files that don't exist on the server yet (an existing,
        different file is left alone and shows up in verify())."""
        extra = ["-rlt", "--partial", "--ignore-existing", "--chmod=Dugo+rx,Fugo+r", "--info=progress2"]
        if not self.local:
            extra.insert(0, "--chown=99:100")
        if not self.local:
            self.run(f"mkdir -p {q(self.path(category))}", timeout=60)
        return self._rsync(extra, source_dir, category, rels, capture=False).returncode

    def verify(self, source_dir: Path, category: str, rels) -> set:
        """Relative paths whose server copy differs from (or lacks) the local file."""
        r = self._rsync(["-rl", "--checksum", "--dry-run", "--itemize-changes"], source_dir, category, rels,
                        capture=True)
        if r.returncode != 0:
            raise RuntimeError("rsync verification failed: " + r.stderr.decode(errors="replace")[-500:])
        differing = set()
        for line in r.stdout.decode(errors="replace").splitlines():
            if len(line) > 12 and line[0] in "<>ch." and line[1] in "fdLDS" and line[1] != "d":
                differing.add(line[12:])
        return differing


def offload_ssh(spec: str, apply: bool = False, categories=blobs.CATEGORIES, jobs: int = 4):
    if not settings.public_url:
        raise RuntimeError("PUBLIC_URL / --public-url is required: it goes into the note links")
    store = SshStore(spec)
    stage("Planning")
    linked = blobs._linked_targets()
    plans = {cat: blobs.plan(cat, linked) for cat in categories}
    git = plans.get("git")

    store.connect()
    try:
        problems = store.problems()
        if problems:
            raise RuntimeError(f"{store.host}: " + "; ".join(problems))
        movable = {root: r for root, r in git["repos"].items() if not r["blocked"]} if git else {}
        existing = store.existing_clones(str(root) for root in movable)

        for cat in categories:
            if cat != "git":
                p = plans[cat]
                log(f"{cat}: copy {len(p['bulk'])} files ({blobs.human(p['bytes'])}) with rsync, keep "
                    f"{len(p['keep'])}, skip {len(p['skipped'])} (changed recently)")
        if git:
            held = len(git["repos"]) - len(movable)
            log(f"git: {len(movable)} clones ({blobs.human(sum(r['bytes'] for r in movable.values()))}) - "
                f"{len(movable) - len(existing)} to re-clone on {store.host}, {len(existing)} already there"
                + (f"; {held} held back" if held else ""))
            for root, r in sorted(git["repos"].items()):
                if r["blocked"]:
                    log(f"  held back ({r['blocked']}): Dumps/git/{root}")
        total = sum(p["bytes"] for p in plans.values())
        if not apply:
            result(f"Dry run: {blobs.human(total)} would move out of the vault to {spec}")
            return plans

        freed = 0
        summary = []
        if git:
            freed_git, counts = _git_over_ssh(store, git, movable, existing, jobs)
            freed += freed_git
            summary.append(f"{counts['recloned']} repos re-cloned on the server, {counts['in_store']} already "
                           f"there, {counts['left']} left in the vault")
        for cat, p in plans.items():
            if cat == "git" or not p["bulk"]:
                continue
            moved, conflicts, freed_cat = _files_over_ssh(store, cat, p)
            freed += freed_cat
            summary.append(f"{len(moved)} {cat} files moved" + (f", {conflicts} conflicts left" if conflicts else ""))
        result(f"Offloaded {blobs.human(freed)} from the vault ({'; '.join(summary) or 'nothing to move'})")
        return plans
    finally:
        store.close()


def _git_over_ssh(store: SshStore, git: dict, movable: dict, existing: set, jobs: int):
    cat_dir = settings.dumps_dir / "git"
    counts = {"recloned": 0, "in_store": 0, "left": len(git["repos"]) - len(movable)}
    freed = 0

    def finalize(root):
        nonlocal freed
        for rel in movable[root]["bulk"]:
            path = cat_dir / rel
            if path.is_symlink() or path.is_file():
                freed += path.lstat().st_size
                path.unlink()
        # README(s) stay in the vault as they are; the marker points at the listing
        (cat_dir / root / blobs.MARKER).write_text(blobs.url("git", str(root), directory=True) + "\n",
                                                   encoding="utf-8")

    todo = []
    for root in sorted(movable):
        if str(root) in existing:
            finalize(root)
            counts["in_store"] += 1
            continue
        remote = blobs.remote_url(cat_dir / root)
        if not remote:
            log(f"left in the vault, no remote URL: Dumps/git/{root}")
            counts["left"] += 1
            continue
        todo.append((root, remote))

    stage(f"Re-cloning {len(todo)} repositories on {store.host} ({jobs} at a time)")
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(store.reclone, remote, str(root)): (root, remote) for root, remote in todo}
        for future in as_completed(futures):
            check_cancelled()
            root, remote = futures[future]
            done += 1
            ok, error = future.result()
            if ok:
                finalize(root)
                counts["recloned"] += 1
                log(f"[{done}/{len(todo)}] re-cloned {root}")
            else:
                counts["left"] += 1
                log(f"[{done}/{len(todo)}] left in the vault, could not re-clone {remote}: {error}")
            progress(done / max(len(todo), 1), f"{done}/{len(todo)} repos")
    blobs._remove_empty_dirs(cat_dir)
    return freed, counts


def _files_over_ssh(store: SshStore, cat: str, p: dict):
    cat_dir = settings.dumps_dir / cat
    stage(f"Copying {cat} to {store.host} with rsync")
    to_copy = list(p["bulk"]) + [r for r in p["keep"] if not r.name.endswith(blobs.MARKER_SUFFIX)]
    if store.copy(cat_dir, cat, to_copy) not in (0, 23, 24):  # 23/24: partial (e.g. vanished files)
        raise RuntimeError(f"rsync copy failed for {cat} - nothing deleted")
    stage(f"Verifying {cat} by checksum")
    differing = store.verify(cat_dir, cat, p["bulk"])
    moved = [rel for rel in p["bulk"] if str(rel) not in differing]
    for rel in sorted(differing):
        log(f"CONFLICT or incomplete, left in the vault: Dumps/{cat}/{rel}")

    stage(f"Updating notes for {cat}")
    freed = 0
    with vault_lock():
        for rel in moved:
            if blobs._is_media(rel):
                folder = cat_dir / rel.parent
                videos = [f for f in folder.iterdir() if f.is_file() and blobs._is_media(f)]
                blobs.write_video_markers(folder, cat, str(rel), only_video_in_folder=len(videos) == 1)
        blobs._rewrite_links({f"Dumps/{cat}/{rel}" for rel in moved if blobs._is_media(rel)})
        for rel in moved:
            path = cat_dir / rel
            if path.is_symlink() or path.is_file():
                freed += path.lstat().st_size
                path.unlink()
    blobs._remove_empty_dirs(cat_dir)
    return moved, len(differing), freed
