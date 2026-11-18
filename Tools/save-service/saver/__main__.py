"""Command line entry point.

  python -m saver serve                       run the web UI + API + workers
  python -m saver save <url> [--force]        save one URL synchronously (debugging)
  python -m saver import-registry <file>      merge a registry JSON export into the database
  python -m saver export-registry [file]      write the registry out as JSON (backup)
  python -m saver offload --ssh USER@HOST:/PATH --public-url URL [--vault DIR] [--apply]
                                              move bulky dumps from a vault into the blob store on
                                              a server: clones run there, videos go over rsync
  python -m saver offload --blobs DIR --public-url URL [--vault DIR] [--apply]
                                              the same into a locally reachable store
"""
import argparse
import json
import sys
from pathlib import Path

from .config import settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="saver", description="Obsidian save service")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the web UI, API and job workers")

    save = sub.add_parser("save", help="save one URL (or a local .pdf/.html) synchronously")
    save.add_argument("target")
    save.add_argument("--force", action="store_true")
    save.add_argument("--allow-thin", action="store_true")
    save.add_argument("--source", help="for a local PDF: the URL it came from")

    imp = sub.add_parser("import-registry", help="merge a registry JSON export into the database")
    imp.add_argument("file", help="a file written by export-registry or GET /api/registry")
    imp.add_argument("--overwrite", action="store_true", help="replace entries that already exist")

    exp = sub.add_parser("export-registry", help="write the registry out as JSON")
    exp.add_argument("file", nargs="?", help="defaults to stdout")

    off = sub.add_parser("offload", help="move bulky dumps (video files, git clones) into the blob store")
    off.add_argument("--vault", help="vault to move from (default: VAULT_DIR)")
    off.add_argument("--blobs", help="blob store to move into, as a local path (default: BLOB_DIR)")
    off.add_argument("--ssh", metavar="USER@HOST:/PATH",
                     help="blob store on a server: clone there and rsync videos over SSH (fast; no mount needed)")
    off.add_argument("--jobs", type=int, default=4, help="parallel server-side clones with --ssh (default 4)")
    off.add_argument("--public-url", help="the service's PUBLIC_URL, written into note links (default: PUBLIC_URL)")
    off.add_argument("--min-age", type=int, help="skip files modified within this many seconds (default 600)")
    off.add_argument("--only", action="append", choices=["git", "youtube", "video"], help="limit to a category")
    off.add_argument("--apply", action="store_true", help="actually copy, verify, rewrite links and delete")

    args = parser.parse_args(argv)

    if args.command == "offload":
        return _offload(args)

    if args.command == "serve":
        import uvicorn
        uvicorn.run("saver.app:app", host=settings.host, port=settings.port, log_level="info")
        return 0

    from .db import get_db
    from .util import ensure_directories

    if args.command == "import-registry":
        path = Path(args.file)
        counts = get_db().import_registry(json.loads(path.read_text(encoding="utf-8")), overwrite=args.overwrite)
        print(f"{path}: {counts}")
        return 0

    if args.command == "export-registry":
        data = json.dumps(get_db().export_registry(), indent=2) + "\n"
        if args.file:
            Path(args.file).write_text(data, encoding="utf-8")
        else:
            sys.stdout.write(data)
        return 0

    if args.command == "save":
        from .errors import SaveError
        from .pipeline import Saver

        ensure_directories()
        target = Path(args.target).expanduser()
        try:
            if target.is_file() and target.suffix.lower() == ".pdf":
                outcome = Saver().import_pdf_file(target, source_url=args.source, force=args.force)
            elif target.is_file() and target.suffix.lower() in (".html", ".htm"):
                outcome = Saver().import_captured_file(target)
            else:
                outcome = Saver().save(args.target, force=args.force, allow_thin=args.allow_thin)
        except SaveError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"outcome: {outcome}")
        return 0
    return 2


def _console_reporter():
    """Prints a job's progress lines to the terminal, at most every 5s."""
    import time

    from .jobctx import Reporter

    class Console(Reporter):
        last = 0.0

        def progress(self, fraction, label=None):
            now = time.monotonic()
            if label and now - self.last > 5:
                self.last = now
                print(f"   {fraction * 100:5.1f}%  {label}" if fraction is not None else f"   {label}", flush=True)

    return Console()


def _offload(args) -> int:
    from pathlib import Path

    from . import blobs
    from .jobctx import bind

    if args.blobs and args.ssh:
        print("ERROR: give either --blobs or --ssh", file=sys.stderr)
        return 2
    overrides = {"vault_dir": Path(args.vault).expanduser()} if args.vault else {}
    if args.blobs:
        overrides["blob_dir"] = str(Path(args.blobs).expanduser())
    if args.public_url:
        overrides["public_url"] = args.public_url.rstrip("/")
    if args.min_age is not None:
        overrides["offload_min_age"] = args.min_age
    settings.update(**overrides)
    categories = tuple(args.only or blobs.CATEGORIES)

    if args.ssh:
        from .remote_offload import offload_ssh
        if not settings.public_url:
            print("ERROR: need --public-url (or PUBLIC_URL): it goes into the note links", file=sys.stderr)
            return 2
        print(f"vault {settings.vault_dir} -> {args.ssh} (links via {settings.public_url})")
        with bind(_console_reporter()):
            offload_ssh(args.ssh, apply=args.apply, categories=categories, jobs=args.jobs)
        if not args.apply:
            print("Dry run only - add --apply to clone on the server, copy, verify, rewrite links and delete.")
        return 0

    if not settings.blobs_enabled:
        print("ERROR: need --blobs and --public-url (or BLOB_DIR / PUBLIC_URL), "
              "and the blob store must not be inside the vault", file=sys.stderr)
        return 2
    print(f"vault {settings.vault_dir} -> store {settings.blob_dir} (links via {settings.public_url})")
    with bind(_console_reporter()):
        blobs.offload(apply=args.apply, categories=categories)
    if not args.apply:
        print("Dry run only - add --apply to copy, verify, rewrite links and delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
