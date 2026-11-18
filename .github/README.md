# obsidian-tools

Self-hosted tooling for an Obsidian vault that collects links:

- **[Save service](../Tools/save-service/)**: a Docker web UI and API that
  turns links into vault entries. It captures pages with SingleFile, clones
  git repos, downloads PDFs and videos (with a Whisper transcript), tags and
  summarizes them with a local LLM through Ollama, and files each one as a
  bullet in an `Areas/` note. Bulky files (videos, full clones) can live in
  a blob store outside the vault, served by the service. The same stack runs
  [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync) so the
  service's writes reach every device.
- **[vault-maintenance](../.claude/skills/vault-maintenance/)**: a
  [Claude Code](https://claude.com/claude-code) skill that audits and
  restructures `Areas/` (duplicates, stale links, misfiled entries,
  oversized folders) and keeps the service's registry in sync.

| Path | |
|---|---|
| `Tools/save-service/` | The service, its `docker-compose.yml` and `.env.example`. Start with its README |
| `Tools/save-service/client/` | `obsidian-save`, a command-line and keybinding client |
| `.claude/skills/vault-maintenance/` | The Claude Code skill |

## Vault layout

The tools assume this layout, and create what's missing:

```
<vault>/
  Areas/<area>/…/<topic>.md    leaf notes; each saved link is one bullet
  Areas/<area>/<area>.md       Waypoint folder notes (generated)
  Dumps/                       captures: single_page/, pdf/, git/, youtube/, video/
  Dumps/singlefile_inbox/      drop SingleFile or PDF files here to import them
```

## Using the repository

Only the service is needed to self-host: copy or clone `Tools/save-service/`
to your server and follow its README.

To also use the skill, root the repository at your vault. `.gitignore`
tracks an allowlist (`Tools/`, `.claude/skills/`, `.github/`), so notes,
dumps and Obsidian settings are never committed:

```sh
cd /path/to/vault
git init && git remote add origin <this repository>
git fetch origin && git checkout -t origin/main
```

## License

Public domain ([The Unlicense](../LICENSE)): do whatever you want with it.
