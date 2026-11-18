# Obsidian save service

A small web UI and HTTP API that turn links into Obsidian vault entries,
running on a server next to a GPU. Each save captures the page with
SingleFile, clones the repo, or downloads the PDF/video (with a Whisper
transcript), then tags and summarizes it with Ollama and appends a bullet to
an `Areas/` leaf note.

- **Web UI**: paste links or drop PDFs / SingleFile pages, then watch the
  queue with live stage and progress bars (download %, transcription
  position, crawl pages) and per-job logs. The dashboard shows stats
  (links saved, by type, per day, top areas), recent results with
  Retry / Re-save / Keep anyway, and a searchable library of everything
  saved.
- **HTTP API** for scripts, a desktop keybinding, a bookmarklet and the
  vault-maintenance skill.
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
- **Updating repos:** *Storage → Update repos* runs `git pull --ff-only` on
  every clone and refreshes the README copies in the vault.
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

## Deploying

`docker-compose.yml` runs everything on the server:

| Service | Purpose |
|---|---|
| `saver` | This service (GPU) |
| `couchdb` | The LiveSync database your devices sync with |
| `livesync-init` | One-shot: LiveSync's own CouchDB provisioning (single node, auth, CORS, size limits) and the database |
| `livesync-bridge` | Keeps the server's vault copy (`VAULT_DIR`) in sync with CouchDB |
| `livesync-setup-uri` | On demand: prints the Setup URI for the first device |

**Why the bridge:** [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync)
is an Obsidian plugin, and it syncs devices through CouchDB, not through
folders. The service isn't Obsidian: it writes entries, transcripts, READMEs
and folder notes into a folder on the server.
[livesync-bridge](https://github.com/vrtmrz/livesync-bridge) turns those
writes into CouchDB changes your devices receive. In the other direction it
turns your edits back into files the service reads (Area notes it appends
to, the SingleFile inbox).

### 1. Prerequisites

- Docker with Compose v2.
- An [Ollama](https://ollama.com) server with the model you configure
  (`OLLAMA_MODEL`, default `qwen3.8:27b`). It can run on the same host
  (`OLLAMA_URL=http://host.docker.internal:11434`).
- Optional but recommended: an NVIDIA GPU with the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/).
  Without one, delete the `deploy:` block of `saver` in
  `docker-compose.yml`; transcription then runs on the CPU. The first
  transcription downloads the Whisper model (about 3 GB for `large-v3`) into
  the service's data folder.
- A reverse proxy with HTTPS and two hostnames pointing at the server, one
  for the service (`PUBLIC_URL`) and one for CouchDB
  (`LIVESYNC_PUBLIC_URL`). The Obsidian mobile app only talks to CouchDB
  over HTTPS. `saver` and `couchdb` join the proxy's external Docker network
  (`PROXY_NETWORK`, default `proxy`), so the proxy reaches them by container
  name. With [Caddy](https://caddyserver.com):
  ```
  saver.example.com {
      reverse_proxy obsidian-saver:8765
  }
  sync.example.com {
      reverse_proxy obsidian-couchdb:5984
  }
  ```
  CouchDB has no host port: it's reachable only through the proxy. If your
  proxy has no Docker network, create one (`docker network create proxy`)
  and attach the proxy to it. If the proxy runs on the host instead of in
  Docker, still create the network, and publish CouchDB on loopback only in
  a `docker-compose.override.yml` (git-ignored):
  ```yaml
  services:
    couchdb:
      ports: ["127.0.0.1:5984:5984"]
  ```

### 2. Configure

Put this folder on the server (clone the repository, or run
`deploy/push.sh user@host /path/on/server` from a checkout; it rsyncs the
source and never touches the server's `.env` or `data/`, and
`PUSH_CHOWN=99:100` sets the files' owner, e.g. for Unraid shares). Keep
`DATA_DIR` outside the source folder if you replace that folder on updates. Then create `.env`
and fill in its three secrets:

```sh
cd /path/to/save-service
cp .env.example .env
sed -i \
  -e "s/^COUCHDB_PASSWORD=$/COUCHDB_PASSWORD=$(openssl rand -hex 24)/" \
  -e "s/^LIVESYNC_PASSPHRASE=$/LIVESYNC_PASSPHRASE=$(openssl rand -hex 24)/" \
  -e "s/^LIVESYNC_ID_KEY=$/LIVESYNC_ID_KEY=sls-id-v1:$(openssl rand -hex 32)/" \
  .env
grep -E '^(COUCHDB_PASSWORD|LIVESYNC_PASSPHRASE|LIVESYNC_ID_KEY)=' .env
```

The `sed` only fills lines that are still empty, so running it again never
replaces a value. Copy what the `grep` prints into your password manager.
Then replace the placeholders in `.env`:

| Variable | |
|---|---|
| `VAULT_DIR` | The server's copy of the vault |
| `BLOBS_DIR` | The blob store: a folder outside the vault |
| `DATA_DIR` | Container state (database, CouchDB, bridge); default `./data` |
| `PUID` / `PGID` | Owner of `VAULT_DIR` (`id -u` / `id -g`); everything written into the vault gets this owner |
| `PROXY_NETWORK` | Your reverse proxy's Docker network |
| `PUBLIC_URL` | The service's HTTPS URL. Written into note links: choose it once |
| `LIVESYNC_PUBLIC_URL` | CouchDB's HTTPS URL |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Your Ollama server and model |

Check the whole file with `docker compose config`, which fails on any
required value that is still missing. `.env.unraid.example` is a filled-in
example: an Unraid server with Caddy on its `main` Docker network.

| Secret | What it is | Notes |
|---|---|---|
| `COUCHDB_PASSWORD` | CouchDB administrator password; the plugin and the bridge log in with it | |
| `LIVESYNC_PASSPHRASE` | End-to-end encryption key: devices and the bridge encrypt everything with it before it reaches CouchDB | The Setup URI carries it, so you never type it on a device. Lost or changed, the database can't be read and has to be rebuilt; the notes themselves stay on every device |
| `LIVESYNC_ID_KEY` | The vault's ID key (`sls-id-v1:` + 64 hex characters). LiveSync derives document and chunk IDs from it, so the database doesn't reveal file names. LiveSync calls it the "recovery code" | Shared by the Setup URI and the bridge, which is why it lives here. Never change it once the first device has set up the database |
| Setup URI passphrase | Protects only the Setup URI | Not in `.env`: `livesync-setup-uri` makes one and prints it once |

Hex values keep `.env` safe: Compose interpolates `$`, and ` #` starts a
comment. To make one by hand: `openssl rand -hex 24` (the ID key:
`echo "sls-id-v1:$(openssl rand -hex 32)"`; in fish,
`"sls-id-v1:"(openssl rand -hex 32)`).

### 3. Start the stack and set up LiveSync

1. Put your current vault at `VAULT_DIR` (e.g. `rsync -a ~/obsidian/
   user@server:/path/to/obsidian-vault/`), owned by `PUID:PGID`. If another
   sync tool also syncs this folder, stop it first: two sync tools on one
   folder replay each other's changes. Disable the **Waypoint** plugin on
   every device (the service writes folder notes now).
2. Start the stack:
   ```sh
   docker compose up -d --build
   docker compose logs -f livesync-bridge
   ```
   The bridge logs *Waiting for the first Obsidian device to initialise...*:
   it has to use exactly the settings that device stores.
3. Print the Setup URI and note its passphrase:
   ```sh
   docker compose run --rm livesync-setup-uri
   ```
4. Set up the **first device with a new, empty vault**, not your real one.
   That way the server's copy fills CouchDB, and your desktop doesn't upload
   the whole vault again. In that empty vault: install *Self-hosted
   LiveSync*, choose *I am setting this up for the first time* → *Use a
   Setup URI*, paste the URI and passphrase, then *Restart and Initialise
   Server* → *Overwrite Server* → *Use this device's settings*.
5. The bridge notices within 15 s, writes its config to match, and uploads
   the server's vault into CouchDB. Watch the log until it settles; a large
   vault takes a while.
6. In the empty vault, run *Self-hosted LiveSync: Copy settings as a new
   Setup URI*. Then add your other devices, choosing *I am adding a device
   to an existing synchronisation setup* → *Use a Setup URI* → *Restart and
   Fetch Data*:
   - **A device whose vault matches the server's copy**: use the existing
     vault. Pick *Compare time and take newer* and *Recreate remote files
     even if they were deleted on remote*. Nothing local gets deleted.
   - **A device whose vault is out of date**: don't use its old vault. Its
     stale and deleted notes would be uploaded back as if they were new.
     Create a new, empty vault there, pick *Overwrite all with remote
     files*, and then copy its `.obsidian` settings over if you want them.
7. Delete the empty vault.

### 4. Finish

- Open `PUBLIC_URL` and save a test link; its job log shows each stage,
  including the calls to Ollama.
- **YouTube cookies** (recommended): in the dashboard's *YouTube cookies*
  card, upload or paste an export from a logged-in browser. Both a Netscape
  `cookies.txt` (e.g. "Get cookies.txt LOCALLY") and Cookie-Editor's JSON
  work. The card shows whether it contains a YouTube login and when that
  expires; cookie values are never displayed. Export from a private window
  and close it afterwards, because YouTube rotates the cookies of sessions
  that stay open. You can also drop the file at `DATA_DIR/saver/cookies.txt`.
- Set up the desktop client (see "Using it").

**Updating:** with this folder in a git checkout on the server, run
`deploy/update.sh` there. It shows the incoming commits, fast-forwards,
builds the images and restarts the stack only if the build succeeded,
asking before each step (`-y` skips the questions). When a stack manager
runs Compose from its own folder (Unraid's Compose Manager), pass that
folder: `deploy/update.sh --project-dir /boot/config/plugins/compose.manager/projects/<name>`.
It then copies a changed `docker-compose.yml` there, and checks that the
folder's `.env` has `STACK_DIR` pointing at the checkout. A checkout that
holds only this folder:

```sh
git clone --sparse --filter=blob:none <repository> /path/to/obsidian-tools
git -C /path/to/obsidian-tools sparse-checkout set Tools/save-service
# STACK_DIR=/path/to/obsidian-tools/Tools/save-service
```

Without git, `deploy/push.sh` copies a local checkout over, followed by
`docker compose up -d --build`. To update yt-dlp alone, restart the
container.

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

The service runs fine on its own against a vault that another tool (e.g.
Syncthing) keeps in sync on the server: run just `docker compose up -d
--build saver` (Compose still checks the whole file, so give the LiveSync
variables in `.env` any non-empty value), or on Unraid use the Docker UI template
(`unraid/obsidian-saver.xml`). For the template, build the image first with
`docker build -t obsidian-saver:latest /path/to/save-service`, copy the
template to
`/boot/config/plugins/dockerMan/templates-user/my-obsidian-saver.xml`, then
use *Add Container → Template: obsidian-saver*. Keep the sync tool's
`.obsidian/workspace*.json` (per-device window layout) out of sync, or it
conflicts constantly.

### Volumes

| Container path | Purpose |
|---|---|
| `/vault` | The server's copy of the vault (`Areas/`, `Dumps/`, `.obsidian/graph.json`) |
| `/config` | `saver.db` (SQLite), `cookies.txt`, `models/` (Whisper), `uploads/`. Keep this out of the vault: syncing a live SQLite database corrupts it |
| `/blobs` | Offsite store for videos and full git clones (see above) |

### Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server (required in the compose file) |
| `OLLAMA_MODEL` | `qwen3.8:27b` | |
| `OLLAMA_THINK` / `OLLAMA_TEMPERATURE` / `OLLAMA_TIMEOUT` | `false` / `0.2` / `180` | |
| `BLOB_DIR` | *(empty)* | Offsite store path inside the container (`/blobs`); empty keeps everything in the vault |
| `PUBLIC_URL` | *(empty)* | How your devices reach the service, e.g. `https://saver.example.com`; required for the store. Written into note links |
| `OFFLOAD_MIN_AGE` | `600` | Seconds a vault file must be untouched before the offload moves it |
| `MANAGE_WAYPOINTS` | `false` | Regenerate Waypoint folder-note indexes here, as the only writer (disable the Obsidian plugin) |
| `WAYPOINT_POLL_SECONDS` | `5` | How often the vault structure is checked for changes |
| `WORKERS` | `2` | Jobs in parallel (transcription is always serialized) |
| `API_TOKEN` | *(empty)* | When set, the UI and API need `Authorization: Bearer <token>`. Set this if the service is reachable from outside your LAN |
| `WHISPER_ENABLED` | `true` | |
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

## Using it

- **Web UI**: paste one or more links (one per line). The options are
  *Re-save if already saved*, *Keep low-content pages* (save pages the LLM
  would reject as having no content of their own, such as tag archives,
  indexes, login walls) and *Crawl site* (depth, max pages, scope, PDFs
  only). Each option is explained in the UI. Drop PDFs or SingleFile `.html`
  files onto the drop zone.
- **Bookmarklet**: drag the *Save to Obsidian* link from the dashboard's
  Service card to your bookmarks bar.
- **Desktop client**: `client/obsidian-save` (Python 3, no dependencies)
  takes the link from the clipboard (`wl-paste`) or a
  [Walker](https://github.com/abenz1267/walker) prompt, queues it, and
  reports the result via `notify-send`, so it works from a keybinding:
  ```sh
  cp client/client.env.example client.env    # set SAVE_SERVICE_URL (and the token)
  ln -sf "$PWD/client/obsidian-save" ~/.local/bin/obsidian-save
  ln -sf "$PWD/client/obsidian-save" ~/.local/bin/obsidian-crawl-site   # crawls by default
  ```
  From a terminal: `obsidian-save URL… [--force] [--thin] [--crawl --depth 2 --pages 30] [--file paper.pdf --source URL] [--wait]`.
  If this folder lives inside your vault, `client.env` is synced along with
  it, so treat the token as shared between your machines.

### API

All endpoints are under `/api` and return JSON. Full schema at `/docs`.

```sh
# queue links (also accepts "url": "...", and a whitespace-separated blob)
curl -X POST $URL/api/links -H 'Content-Type: application/json' \
     -d '{"urls": ["https://example.com/post", "https://github.com/rxi/json.lua"], "force": false, "allow_thin": false}'
# crawl
curl -X POST $URL/api/links -H 'Content-Type: application/json' \
     -d '{"url": "https://site.org/docs/", "crawl": {"max_depth": 2, "max_pages": 30, "scope": "path"}}'
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
| `GET` / `POST` / `DELETE` | `/api/cookies` | Cookie status (no values) / upload (`file` or `text` form field, Netscape or JSON) / remove |
| `GET` | `/api/registry` | The whole registry as one JSON object (a backup `import` reads back) |
| `POST` | `/api/registry/patch` | `[{"key", "set": {…}, "unset": […]}]`, used by `fix_registry.py` |
| `POST` | `/api/registry/import?overwrite=` | Merge a registry export |
| `POST`/`DELETE` | `/api/vault-lock`, `/api/vault-lock/{lease}` | Lease that pauses Area-note writes (vault-maintenance) |
| `GET` | `/api/storage` | Blob store status (path, free space, public URL) |
| `POST` | `/api/storage/update-repos` | Queue `git pull` for every stored clone |
| `POST` | `/api/waypoints/refresh` | Regenerate stale Waypoint indexes now |
| `GET` | `/blobs/…`, `/watch/…`, `/subs/…` | Stored files and listings, video player, subtitles as WebVTT (no token) |

### Backing up the registry

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

Keep virtualenvs, caches and `*.egg-info` out of this folder if it lives
inside a synced vault. Code layout:

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
