import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saver import db as dbmod  # noqa: E402
from saver import llm  # noqa: E402
from saver.config import settings  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway vault + config dir with a fresh database, and an LLM stub
    that answers deterministically (no network)."""
    vault = tmp_path / "vault"
    config = tmp_path / "config"
    (vault / "Areas" / "programming").mkdir(parents=True)
    (vault / "Areas" / "programming" / "python.md").write_text(
        "- #python #tooling\n\tExisting entry.\n\thttps://example.org/old\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    saved = {f: getattr(settings, f) for f in ("vault_dir", "config_dir", "api_token", "whisper_enabled",
                                                 "inbox_watch")}
    settings.update(vault_dir=vault, config_dir=config, api_token="", whisper_enabled=False,
                    inbox_watch=False)
    database = dbmod.Database(config / "saver.db")
    dbmod.set_db(database)

    calls = {"area": "programming/web-scraping.md", "summary": "A page about scraping.", "has_content": True}

    def fake_call_raw(prompt):
        import json
        if '{"tags"' in prompt:
            return json.dumps({"tags": ["web", "scraping"]})
        if '"has_content"' in prompt:
            return json.dumps({"summary": calls["summary"], "has_content": calls["has_content"]})
        if '{"summary"' in prompt:
            return json.dumps({"summary": "plain " + calls["summary"]})
        if '{"title"' in prompt:
            return json.dumps({"title": calls.get("title", "Scraping Guide")})
        if '{"path"' in prompt:
            return json.dumps({"path": calls["area"]})
        if '"is_repo"' in prompt:
            return json.dumps({"is_repo": False})
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(llm, "call_raw", fake_call_raw)
    yield {"vault": vault, "config": config, "db": database, "llm": calls}
    database.close()
    dbmod.set_db(None)
    settings.update(**saved)
