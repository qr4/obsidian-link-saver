"""Runtime settings, all read from environment variables.

Everything the service writes lives in one of two places:
  VAULT_DIR   the Obsidian vault (notes, Dumps/, graph.json) - mount the
              server's synced copy of the vault here
  CONFIG_DIR  service state that must NOT live in a synced folder: the
              SQLite database (a sync tool would happily sync a half-written
              WAL), cookies, Whisper models, uploads
"""
import os
import shlex
from dataclasses import dataclass, field, fields
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


@dataclass
class Settings:
    vault_dir: Path = field(default_factory=lambda: Path(_env("VAULT_DIR", str(Path.home() / "obsidian"))))
    config_dir: Path = field(default_factory=lambda: Path(_env("CONFIG_DIR", "/config")))
    # Name of the vault in Obsidian, for obsidian:// links in the web UI.
    vault_name: str = field(default_factory=lambda: _env("VAULT_NAME", "obsidian"))

    # Offsite blob store for the bulky dumps (full git clones, video files).
    # When set, those are written here instead of the vault, served by this
    # service under /blobs, and the vault only keeps READMEs, transcripts and
    # .offloaded markers. Requires PUBLIC_URL, because notes link to it.
    blob_dir: str = field(default_factory=lambda: _env("BLOB_DIR", ""))
    # How this service is reached from your devices, e.g.
    # https://saver.example.com. Baked into note links - keep it stable.
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "").rstrip("/"))
    # Files younger than this are left alone by `python -m saver offload` (a save or a
    # sync transfer may still be writing them).
    offload_min_age: int = field(default_factory=lambda: _int("OFFLOAD_MIN_AGE", 600))

    # Regenerate Waypoint folder-note indexes here, as the only writer. Turn
    # the Waypoint plugin off in Obsidian when enabling this - two writers on
    # different machines are what causes sync conflicts on folder notes.
    manage_waypoints: bool = field(default_factory=lambda: _bool("MANAGE_WAYPOINTS", False))
    waypoint_poll_seconds: float = field(default_factory=lambda: _float("WAYPOINT_POLL_SECONDS", 5.0))

    # HTTP server
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8765))
    # When set, every /api call needs `Authorization: Bearer <token>`.
    api_token: str = field(default_factory=lambda: _env("API_TOKEN", ""))

    # Job processing
    workers: int = field(default_factory=lambda: _int("WORKERS", 2))
    inbox_watch: bool = field(default_factory=lambda: _bool("INBOX_WATCH", True))
    inbox_poll_seconds: float = field(default_factory=lambda: _float("INBOX_POLL_SECONDS", 3.0))

    # LLM (Ollama)
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen3.8:27b"))
    # skip the model's <think> reasoning pass - we just want fast, structured JSON
    ollama_think: bool = field(default_factory=lambda: _bool("OLLAMA_THINK", False))
    # keep tagging/summarizing deterministic-ish
    ollama_temperature: float = field(default_factory=lambda: _float("OLLAMA_TEMPERATURE", 0.2))
    ollama_timeout: int = field(default_factory=lambda: _int("OLLAMA_TIMEOUT", 180))

    # Page capture (SingleFile CLI driving the bundled Chromium)
    singlefile_bin: str = field(default_factory=lambda: _env("SINGLEFILE_BIN", "single-file"))
    chromium_bin: str = field(default_factory=lambda: _env("CHROMIUM_BIN", "/usr/bin/chromium"))
    singlefile_timeout: int = field(default_factory=lambda: _int("SINGLEFILE_TIMEOUT", 180))

    # yt-dlp. YouTube increasingly demands a logged-in session; export one
    # with a "cookies.txt" browser extension and drop it at this path.
    ytdlp_cookies: str = field(default_factory=lambda: _env("YTDLP_COOKIES", ""))
    ytdlp_extra_args: list = field(default_factory=lambda: shlex.split(_env("YTDLP_EXTRA_ARGS", "")))

    # Whisper (faster-whisper)
    whisper_enabled: bool = field(default_factory=lambda: _bool("WHISPER_ENABLED", True))
    # biggest/most accurate Whisper size; slower than "small" but worth it on a real GPU
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "large-v3"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "auto"))
    # "auto" = float16 on CUDA, int8 on CPU. int8_float16 roughly halves VRAM.
    whisper_compute_type: str = field(default_factory=lambda: _env("WHISPER_COMPUTE_TYPE", "auto"))
    whisper_batch_size: int = field(default_factory=lambda: _int("WHISPER_BATCH_SIZE", 8))
    # Free the GPU after this many idle seconds - Ollama shares the card.
    whisper_unload_after: int = field(default_factory=lambda: _int("WHISPER_UNLOAD_AFTER", 300))

    http_user_agent: str = "Mozilla/5.0 (compatible; obsidian-save-service/1.0)"

    # ---- derived paths -------------------------------------------------
    @property
    def dumps_dir(self) -> Path:
        return self.vault_dir / "Dumps"

    @property
    def page_dump_dir(self) -> Path:
        return self.dumps_dir / "single_page"

    @property
    def areas_dir(self) -> Path:
        return self.vault_dir / "Areas"

    @property
    def git_dir(self) -> Path:
        return self.dumps_dir / "git"

    @property
    def youtube_dir(self) -> Path:
        return self.dumps_dir / "youtube"

    @property
    def video_dir(self) -> Path:
        # Non-YouTube video hosts yt-dlp can handle (media.ccc.de today). Kept
        # apart from youtube_dir because that tree is laid out channel/video,
        # which doesn't describe a conference talk.
        return self.dumps_dir / "video"

    @property
    def pdf_dir(self) -> Path:
        return self.dumps_dir / "pdf"

    @property
    def singlefile_inbox_dir(self) -> Path:
        # SingleFile Companion's savePath - pages captured in a real
        # logged-in browser session and synced over to the server.
        return self.dumps_dir / "singlefile_inbox"

    @property
    def graph_config_path(self) -> Path:
        return self.vault_dir / ".obsidian" / "graph.json"

    @property
    def blob_root(self):
        return Path(self.blob_dir) if self.blob_dir else None

    @property
    def blob_dir_inside_vault(self) -> bool:
        """A store inside the synced vault would be synced right back."""
        if not self.blob_dir:
            return False
        store, vault = Path(self.blob_dir).resolve(), self.vault_dir.resolve()
        return store == vault or vault in store.parents

    @property
    def blobs_enabled(self) -> bool:
        return bool(self.blob_dir and self.public_url) and not self.blob_dir_inside_vault

    @property
    def db_path(self) -> Path:
        return self.config_dir / "saver.db"

    @property
    def lock_path(self) -> Path:
        return self.config_dir / "vault.lock"

    @property
    def upload_dir(self) -> Path:
        return self.config_dir / "uploads"

    @property
    def whisper_model_dir(self) -> Path:
        return self.config_dir / "models"

    @property
    def cookies_path(self):
        path = Path(self.ytdlp_cookies) if self.ytdlp_cookies else self.config_dir / "cookies.txt"
        return path if path.is_file() else None

    def update(self, **overrides):
        """Replace settings in place (tests, CLI flags). Everything reads
        `settings.<name>` at call time, so this takes effect immediately."""
        names = {f.name for f in fields(self)}
        for key, value in overrides.items():
            if key not in names:
                raise AttributeError(f"unknown setting {key!r}")
            setattr(self, key, value)


settings = Settings()
