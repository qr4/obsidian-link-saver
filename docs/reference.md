# Reference

In-depth documentation for obsidian-link-saver. Setting it up is covered in
the [README](../README.md#bootstrap).

## What the service does

- **Web UI**: paste links or drop PDFs / SingleFile pages, then watch the
  queue with live stage and progress bars (download %, transcription
  position, crawl pages) and per-job logs. The dashboard shows stats
  (links saved, by type, per day, top areas), recent results with
  Retry / Re-save / Keep anyway, and a searchable library of everything
  saved. The options are *Re-save if already saved*, *Keep low-content
  pages* (save pages the LLM would reject as having no content of their
  own, such as tag archives or login walls) and *Crawl site* (depth, max
  pages, scope, PDFs only).
- **HTTP API** for scripts, the client, the bookmarklet and the
  vault-maintenance skill (see [API](#api)).
- **SQLite registry** of everything saved, so a link is never saved twice.
- **Job queue**: parallel workers, cancel, retry, pause (which survives
  restarts), and recovery of jobs interrupted by a restart.
- **Crawling is a job type**: each discovered page becomes its own job.
- **Inbox**: SingleFile Companion captures and PDFs dropped into
  `Dumps/singlefile_inbox` are imported automatically.
- **Whisper**: faster-whisper runs in-process on the GPU, one transcription
  at a time. The model is unloaded after 5 idle minutes so Ollama gets the
  VRAM back. Transcripts are written as `out.srt/.vtt/.txt/.json`.

## Offsite storage for videos and git clones

Video files and full git clones quickly add up to tens of gigabytes that
every device would have to sync. With `BLOB_DIR` and `PUBLIC_URL` set (the
compose file sets both), those go into a blob store outside the vault: a
separate folder or share, never one inside the synced vault. The service
serves the store, and the vault keeps only what is small and worth
searching:

| | In the blob store | In the vault |
|---|---|---|
| Git repo | The full clone | Root README(s), whole gists, `.offloaded` (URL of the clone's file listing) |
| Video | The `.mkv`/`.mp4` | All transcripts and subtitles (`out.srt/.vtt/.txt/.json`, yt-dlp's `.srt`), `.offloaded` (the video's watch URL) |

Area notes then link like this:

```
[[Dumps/git/github.com/rxi/log.c/README.md]] · [Browse files](https://…/blobs/git/github.com/rxi/log.c/)
[▶ Watch](https://…/watch/youtube/Chan/Talk [id]/Talk [id].mkv) · [[Dumps/youtube/Chan/Talk [id]/out.txt|Transcript]]
```

- `/blobs/<path>`: files, with Range support so videos can seek, plus
  browsable folder listings that show the README.
- `/watch/<path>`: an HTML5 player with the Whisper and YouTube subtitles as
  tracks, and a link to the original video. `?t=754` or `?t=12:34` starts at
  a timestamp.
- These routes are not behind `API_TOKEN`, because links open them straight
  from Obsidian. Keep the hostname on your LAN, or restrict it at the proxy.
  Stored files are served with `Content-Security-Policy: sandbox`, so HTML
  from a cloned repo can't script against the API.
- **Updating repos:** *Storage → Update repos* fast-forwards every clone and
  refreshes the README copies in the vault. File-mode differences are
  ignored. Edits to tracked files that block an update are moved to
  `git stash` in that clone; untracked files are left alone.
- `PUBLIC_URL` is written into note links, so choose a stable name (e.g.
  `https://saver.example.com`). Without `BLOB_DIR` and `PUBLIC_URL` the
  store stays off and everything goes into the vault.

### Moving existing dumps into the store

If the vault already holds videos and clones (saved before the store was
on, or by other tools using the same `Dumps/` layout), move them once from
the command line on the machine holding the vault:

```sh
python3 -m saver offload --ssh user@server:/path/to/obsidian-blobs \
    --vault ~/obsidian --public-url https://saver.example.com     # dry run; add --apply
```

Git clones are re-cloned from their remotes on the server (4 at a time,
`--jobs`). Video files go over with rsync and are verified by checksum.
Note links to moved videos are rewritten, markers are written, and only then
is the vault copy deleted. A repo that can't be re-cloned, and a server file
that differs from the vault's, stay in the vault and are listed. Files
changed within `--min-age` (10 min) wait for a later run. `--blobs DIR` does
the same into a locally reachable store, and
`docker exec -u <PUID>:<PGID> obsidian-saver python -m saver offload --apply`
runs it inside the container. The layout is
`<store>/<git|youtube|video>/<path under Dumps>`.

## Waypoint folder notes (no sync conflicts)

The [Waypoint](https://github.com/IdreesInc/Waypoint) plugin regenerates a
folder note's index whenever something in that folder changes, on every
machine where Obsidian is open, including for changes that arrive through
sync. Two machines (or one Obsidian plus a script) end up modifying the same
folder note before they sync, and you get conflict copies.

With `MANAGE_WAYPOINTS=true` (set in the compose file) the service is the
only writer:

- It regenerates indexes right after every save, and whenever the folder
  structure changes (notes added, moved or deleted, from any machine). It
  waits a few seconds for a sync batch to settle first.
- It writes a folder note only when its index actually changes.
- The output reproduces the plugin's own (2.1.0, default settings), so notes
  look the same and the plugin can be switched back on at any time. The one
  difference is that sync conflict copies are not listed.
- **Turn the Waypoint plugin off** in Obsidian (Settings → Community plugins)
  on every device. The dashboard's Service card warns while the plugin is
  still on in the server's copy.
- `POST /api/waypoints/refresh` regenerates now. The skill's `waypoints.py`
  calls it instead of writing locally.

## LiveSync and the bridge

**Why the bridge:** [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync)
is an Obsidian plugin, and it syncs devices through CouchDB, not through
folders. The service isn't Obsidian: it writes entries, transcripts, READMEs
and folder notes into a folder on the server.
[livesync-bridge](https://github.com/vrtmrz/livesync-bridge) turns those
writes into CouchDB changes your devices receive. In the other direction it
turns your edits back into files the service reads (Area notes it appends
to, the SingleFile inbox).

| Service | Purpose |
|---|---|
| `saver` | The save service |
| `couchdb` | The LiveSync database your devices sync with |
| `livesync-init` | One-shot on every `up`: LiveSync's own CouchDB provisioning (single node, auth, CORS, size limits) and the database |
| `livesync-bridge` | Keeps the server's vault copy (`VAULT_DIR`) in sync with CouchDB. Waits for the first device's settings, then writes its config from them |
| `livesync-setup-uri` | On demand (`docker compose run --rm livesync-setup-uri`): prints the Setup URI for the first device |

| Secret | What it is | Notes |
|---|---|---|
| `COUCHDB_PASSWORD` | CouchDB administrator password; the plugin and the bridge log in with it | |
| `LIVESYNC_PASSPHRASE` | End-to-end encryption key: devices and the bridge encrypt everything with it before it reaches CouchDB | The Setup URI carries it, so you never type it on a device. Lost or changed, the database can't be read and has to be rebuilt; the notes themselves stay on every device |
| `LIVESYNC_ID_KEY` | The vault's ID key (`sls-id-v1:` + 64 hex characters). LiveSync derives document and chunk IDs from it, so the database doesn't reveal file names. LiveSync calls it the "recovery code" | Shared by the Setup URI and the bridge, which is why it lives in `.env`. Never change it once the first device has set up the database |
| Setup URI passphrase | Protects only the Setup URI | Not in `.env`: `livesync-setup-uri` makes one and prints it once |

Hex values keep `.env` safe: Compose interpolates `$`, and ` #` starts a
comment. To make one by hand: `openssl rand -hex 24` (the ID key:
`echo "sls-id-v1:$(openssl rand -hex 32)"`).

### Checking the bridge's upload

The bridge's log goes quiet both when it's finished and when it has skipped
files, so compare counts. Files in CouchDB (replace `obsidian` with your
`LIVESYNC_DB`):

```sh
docker exec obsidian-couchdb sh -c 'curl -s -u "$COUCHDB_USER:$COUCHDB_PASSWORD" http://127.0.0.1:5984/obsidian/_all_docs' | grep -o '"id":"f:' | wc -l
```

Files the bridge should sync (no dot paths):

```sh
cd "$VAULT_DIR" && find . -mindepth 1 -name ".*" -prune -o -name __pycache__ -prune -o -type f -print | wc -l
```

If CouchDB has fewer, typically after the bridge crashed mid-upload, it has
recorded files as scanned that it never sent. Clear its scan state and it
rescans everything. Files already in CouchDB are skipped, and nothing is
deleted:

```sh
docker stop obsidian-livesync-bridge
mkdir -p "$DATA_DIR/livesync/state-backup"
mv "$DATA_DIR"/livesync/state/* "$DATA_DIR/livesync/state-backup/"
docker start obsidian-livesync-bridge
```

### What LiveSync doesn't carry

- Dot files and folders: `.obsidian` (settings and plugins stay per device
  unless you enable the plugin's *Customization sync*), and the service's
  per-repo `.offloaded` markers, which stay on the server.
- Changes made on a desktop while Obsidian is closed sync the next time it
  opens.
- Text in encodings other than UTF-8. LiveSync decodes `.md`, `.txt`,
  `.html`, `.csv`, `.svg`, `.css`, `.js`, `.xml` and `.canvas` as UTF-8, so
  in any other encoding every non-ASCII character becomes `�` on every
  device, and the plugin reports the file as corrupted. The service converts
  repo READMEs and gists to UTF-8 as it copies them in; the vault-maintenance
  skill's `diag.py` lists any text file that still isn't UTF-8.
- The bridge's file watcher can miss a file written right after its folder
  was created. The bridge restarts every `BRIDGE_RESCAN_HOURS` (default 1),
  and the rescan picks such files up.
- To keep large HTML captures and PDFs out of LiveSync, set
  `BRIDGE_IGNORE=Dumps/single_page/**,Dumps/pdf/**` and add the same folders
  to the plugin's ignore settings on each device.

### Without LiveSync

The service runs fine on its own, against a vault on the same machine or
one another tool keeps in sync. The README's
[Without LiveSync](../README.md#without-livesync-an-existing-obsidian-setup)
section describes the setup.

## Volumes

| Container path | Purpose |
|---|---|
| `/vault` | The server's copy of the vault (`Areas/`, `Dumps/`, `.obsidian/graph.json`) |
| `/config` | `saver.db` (SQLite), `cookies.txt`, `settings.json` (what the Settings tab changed), `models/` (Whisper), `uploads/`. Keep this out of the vault: syncing a live SQLite database corrupts it |
| `/blobs` | Offsite store for videos and full git clones (see above) |

## Configuration (environment variables)

The LLM and Whisper settings (`LLM_*`, `OLLAMA_*`, `WHISPER_*`) can also be
changed in the web UI's **Settings** tab. Those changes are stored in
`/config/settings.json` and win over the environment; *Reset to
environment* drops them again.

| Variable | Default | |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama`, `openai` (OpenAI or any OpenAI-compatible server: LM Studio, llama.cpp, vLLM, OpenRouter…) or `anthropic`. [Details](llm.md#talking-to-the-llm) |
| `LLM_URL` | *(provider default)* | Base URL; empty means `OLLAMA_URL` for Ollama, `https://api.openai.com/v1`, `https://api.anthropic.com` |
| `LLM_MODEL` | `OLLAMA_MODEL` | Model name |
| `LLM_API_KEY` | *(empty)* | For hosted APIs |
| `LLM_TEMPERATURE` / `LLM_TIMEOUT` | `0.2` / `180` | Older names `OLLAMA_TEMPERATURE` / `OLLAMA_TIMEOUT` still work. `LLM_TEMPERATURE=none` sends none |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server (`http://host.docker.internal:11434` in the compose files) |
| `OLLAMA_MODEL` | `qwen3.8:27b` | Model when `LLM_MODEL` is unset |
| `OLLAMA_THINK` | `false` | Ollama only |
| `OLLAMA_NUM_CTX` | `32768` | Ollama only. Context window per request, in tokens. Filing a link sends your whole `Areas/` tree (about 22,000 tokens at 1,500 notes); raise it for a bigger vault, `0` leaves it to Ollama |
| `BLOB_DIR` | *(empty)* | Offsite store path inside the container (`/blobs`); empty keeps everything in the vault |
| `PUBLIC_URL` | *(empty)* | How your devices reach the service, e.g. `https://saver.example.com`; required for the store. Written into note links |
| `OFFLOAD_MIN_AGE` | `600` | Seconds a vault file must be untouched before the offload moves it |
| `MANAGE_WAYPOINTS` | `false` | Regenerate Waypoint folder-note indexes here, as the only writer (disable the Obsidian plugin) |
| `WAYPOINT_POLL_SECONDS` | `5` | How often the vault structure is checked for changes |
| `WORKERS` | `2` | Jobs in parallel (transcription is always serialized) |
| `API_TOKEN` | *(empty)* | When set, the UI and API need `Authorization: Bearer <token>`. Set this if the service is reachable from outside your LAN |
| `WHISPER_ENABLED` | `true` | |
| `WHISPER_DEVICE` | `auto` | `cuda` or `cpu` to force one |
| `WHISPER_MODEL` | `large-v3` | Any faster-whisper model, e.g. `large-v3-turbo` for speed |
| `WHISPER_COMPUTE_TYPE` | `auto` | `float16` on GPU, `int8` on CPU; `int8_float16` halves VRAM |
| `WHISPER_BATCH_SIZE` | `8` | Lower it if VRAM is tight |
| `WHISPER_UNLOAD_AFTER` | `300` | Idle seconds before the model is freed (0 = keep loaded) |
| `INBOX_WATCH` / `INBOX_POLL_SECONDS` | `true` / `3` | Import files from `Dumps/singlefile_inbox` |
| `YTDLP_AUTO_UPDATE` | `false` | `pip install -U yt-dlp` on every start (the compose file enables it) |
| `YTDLP_EXTRA_ARGS` | `--js-runtimes node` | Extra yt-dlp flags |
| `YTDLP_COOKIES` | `/config/cookies.txt` | |
| `VAULT_NAME` | `obsidian` | Vault name for `obsidian://` links in the library view |
| `PUID` / `PGID` / `UMASK` | `1000` / `1000` / `002` | Owner and umask of the files the service writes |
| `TZ` | UTC | Timestamps in notes and stats |

## API

All endpoints are under `/api` and return JSON. Full schema at `/docs`.
Requests that a browser sends from another site (an `Origin` header naming
a host other than this service's own) are refused, with or without
`API_TOKEN`; scripts send no `Origin` and aren't affected.

```sh
# queue links (also accepts "url": "...", and a whitespace-separated blob)
curl -X POST $URL/api/links -H 'Content-Type: application/json' \
     -d '{"urls": ["https://example.com/post", "https://github.com/rxi/json.lua"], "force": false, "allow_thin": false}'
# crawl
curl -X POST $URL/api/links -H 'Content-Type: application/json' \
     -d '{"url": "https://site.org/docs/", "crawl": {"max_depth": 2, "max_pages": 30, "scope": "path", "extra_hosts": ["blog.site.org"]}}'
# upload files (optional "source" = original URL of a single PDF)
curl -F files=@paper.pdf -F source=https://example.org/paper.pdf $URL/api/upload
```

| Method | Path | |
|---|---|---|
| `POST` | `/api/links` | Queue URLs. Per-URL result: `queued`, `already_queued`, `already_saved` (with the note) or `invalid` |
| `POST` | `/api/upload` | Upload `.pdf` / `.html` files |
| `GET` | `/api/state` | Everything the dashboard shows, in one call |
| `GET` | `/api/stats` | Counts by type and day, top areas, job counts |
| `GET` | `/api/jobs?status=failed,done&limit=` | Top-level jobs (`parent_id=` for a crawl's children) |
| `GET` | `/api/jobs/{id}?after=<log id>` | Job, log lines and crawl children |
| `POST` | `/api/jobs/{id}/cancel` | Cancel (stops a running download or transcription within seconds) |
| `POST` | `/api/jobs/{id}/retry?force=&allow_thin=` | Run a finished job again |
| `DELETE` | `/api/jobs` | Clear finished jobs |
| `POST` | `/api/pause`, `/api/resume` | Stop/start picking up new jobs (running ones finish) |
| `GET` | `/api/entries?q=&kind=` | Search saved entries |
| `GET` / `PUT` | `/api/settings` | LLM and Whisper settings (the API key only as set/not set) / change them: `{"values": {…}, "reset": [names]}` |
| `POST` | `/api/settings/llm/models`, `/api/settings/llm/test` | List the provider's models / send a test prompt, with unsaved `{"values": {…}}` |
| `GET` / `POST` / `DELETE` | `/api/cookies` | Cookie status (no values) / upload (`file` or `text` form field, Netscape or JSON) / remove |
| `GET` | `/api/registry` | The whole registry as one JSON object (a backup `import` reads back) |
| `POST` | `/api/registry/patch` | `[{"key", "set": {…}, "unset": […]}]`, used by `fix_registry.py` |
| `POST` | `/api/registry/import?overwrite=` | Merge a registry export |
| `POST`/`DELETE` | `/api/vault-lock`, `/api/vault-lock/{lease}` | Lease that pauses Area-note writes (vault-maintenance) |
| `GET` | `/api/storage` | Blob store status (path, free space, public URL) |
| `POST` | `/api/storage/update-repos` | Queue `git pull` for every stored clone |
| `POST` | `/api/waypoints/refresh` | Regenerate stale Waypoint indexes now |
| `GET` | `/blobs/…`, `/watch/…`, `/subs/…` | Stored files and listings, video player, subtitles as WebVTT (no token) |

## Backing up the registry

The registry (which links are saved, and where) lives in `saver.db`.
Besides copying `DATA_DIR/saver/`, you can export it as JSON and import it
into another instance. Run writing commands as `PUID:PGID`, so the database
files keep the service's owner:

```sh
docker exec obsidian-saver python -m saver export-registry > registry-backup.json
docker cp registry-backup.json obsidian-saver:/tmp/
docker exec -u <PUID>:<PGID> obsidian-saver \
    python -m saver import-registry /tmp/registry-backup.json   # --overwrite to replace
```

## Development

```sh
uv venv /tmp/saver-venv && VIRTUAL_ENV=/tmp/saver-venv uv pip install -e ".[test,whisper]"
PYTHONDONTWRITEBYTECODE=1 /tmp/saver-venv/bin/python -m pytest -q
VAULT_DIR=/tmp/test-vault CONFIG_DIR=/tmp/test-config /tmp/saver-venv/bin/python -m saver serve
python -m saver save <url> [--force]   # one synchronous save, for debugging
```

Code layout (`saver/`):

| Module | |
|---|---|
| `pipeline.py` | The save flow per content type |
| `llm.py` | Ollama prompts: tags, summary, title, Area, repo detection |
| `media.py`, `transcribe.py` | yt-dlp downloads with progress; faster-whisper |
| `fetch.py`, `gitrepos.py` | SingleFile capture, PDFs, git clones |
| `vaultnotes.py` | Area bullets, folder notes, graph colors, tag census |
| `crawl.py` | Link discovery and relocation into per-domain folders |
| `waypoints.py` | Waypoint-compatible folder-note indexes and the single-writer keeper |
| `blobs.py`, `blobserve.py` | Offsite store: where files go, vault mirrors, offload, repo updates; serving |
| `remote_offload.py` | Offload over SSH (server-side clones, rsync for videos) |
| `db.py` | SQLite: registry, jobs, logs, stats |
| `jobs.py`, `jobctx.py`, `proc.py` | Workers, progress reporting, cancellable subprocesses |
| `app.py`, `static/` | API and web UI |
