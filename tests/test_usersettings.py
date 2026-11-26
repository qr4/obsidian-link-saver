import json

import pytest
from fastapi.testclient import TestClient

from saver import llm, usersettings
from saver.app import create_app
from saver.config import settings

ENV_NAMES = ("LLM_PROVIDER", "LLM_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TEMPERATURE", "LLM_TIMEOUT",
             "OLLAMA_URL", "OLLAMA_MODEL", "OLLAMA_TEMPERATURE", "OLLAMA_TIMEOUT", "OLLAMA_THINK",
             "OLLAMA_NUM_CTX", "WHISPER_ENABLED", "WHISPER_MODEL", "WHISPER_DEVICE",
             "WHISPER_COMPUTE_TYPE", "WHISPER_BATCH_SIZE", "WHISPER_UNLOAD_AFTER")


@pytest.fixture
def clean_env(env, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    settings.update(llm_model="env-model", llm_provider="ollama", llm_api_key="", whisper_model="large-v3")
    return env


def stored():
    return json.loads(settings.user_settings_path.read_text())


def test_old_env_names_still_work(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "old")
    monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.5")
    from saver.config import Settings
    s = Settings()
    assert (s.llm_provider, s.llm_model, s.llm_temperature, s.llm_base_url) == \
        ("ollama", "old", 0.5, "http://localhost:11434")
    monkeypatch.setenv("LLM_MODEL", "new")
    monkeypatch.setenv("LLM_TEMPERATURE", "none")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    s = Settings()
    assert (s.llm_model, s.llm_temperature, s.llm_base_url) == ("new", None, "https://api.anthropic.com")


def test_save_overrides_and_drops_values_equal_to_env(clean_env):
    usersettings.save({"llm_provider": "openai", "llm_model": "gpt", "whisper_model": "small",
                       "whisper_batch_size": "4", "llm_temperature": ""})
    assert (settings.llm_provider, settings.llm_model, settings.whisper_model) == ("openai", "gpt", "small")
    assert settings.whisper_batch_size == 4 and settings.llm_temperature is None
    assert stored() == {"llm_provider": "openai", "llm_model": "gpt", "whisper_model": "small",
                        "whisper_batch_size": 4, "llm_temperature": None}
    assert oct(settings.user_settings_path.stat().st_mode & 0o777) == "0o600"

    # back to the environment's value: dropped, so a later env change applies
    usersettings.save({"llm_model": "env-model"})
    assert "llm_model" not in stored() and settings.llm_model == "env-model"

    usersettings.save({}, reset=["whisper_model"])
    assert "whisper_model" not in stored() and settings.whisper_model == "large-v3"


def test_invalid_values_change_nothing(clean_env):
    with pytest.raises(usersettings.SettingsError):
        usersettings.save({"llm_model": "x", "llm_provider": "bogus"})
    with pytest.raises(usersettings.SettingsError):
        usersettings.save({"vault_dir": "/etc"})
    with pytest.raises(usersettings.SettingsError):
        usersettings.save({"whisper_batch_size": 0})
    assert settings.llm_model == "env-model" and not settings.user_settings_path.exists()


def test_load_applies_the_file_and_skips_bad_entries(clean_env):
    settings.user_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.user_settings_path.write_text(json.dumps({"llm_model": "from-file", "whisper_device": "gpu",
                                                       "api_token": "x"}))
    assert usersettings.load() == {"llm_model": "from-file"}
    assert settings.llm_model == "from-file" and settings.api_token == ""


def test_api_key_is_never_returned(clean_env):
    usersettings.save({"llm_api_key": "sk-secret"})
    assert settings.llm_api_key == "sk-secret"
    info = usersettings.describe()
    assert info["fields"]["llm_api_key"] == {"value": True, "env": False, "overridden": True}
    assert "sk-secret" not in json.dumps(info)
    usersettings.save({"llm_api_key": "", "llm_model": "m"})  # empty keeps it
    assert settings.llm_api_key == "sk-secret"
    usersettings.save({}, reset=["llm_api_key"])
    assert settings.llm_api_key == ""


def test_api(clean_env, monkeypatch):
    client = TestClient(create_app(run_lifecycle=False))
    info = client.get("/api/settings").json()
    assert info["fields"]["llm_model"]["value"] == "env-model" and "anthropic" in info["choices"]["llm_provider"]

    r = client.put("/api/settings", json={"values": {"whisper_model": "medium", "llm_api_key": "k"}})
    assert r.status_code == 200 and r.json()["fields"]["whisper_model"]["overridden"]
    assert "\"k\"" not in r.text
    assert client.put("/api/settings", json={"values": {"llm_timeout": "soon"}}).status_code == 400
    assert client.get("/api/state").json()["config"]["llm_model"] == "env-model"

    seen = {}

    def fake_complete(prompt, cfg):
        seen.update(provider=cfg.llm_provider, model=cfg.llm_model, key=cfg.llm_api_key)
        return '{"ok": true}'

    monkeypatch.setattr(llm, "complete", fake_complete)
    r = client.post("/api/settings/llm/test", json={"values": {"llm_provider": "anthropic", "llm_model": "c",
                                                               "llm_api_key": ""}})
    assert r.json()["json"] is True
    # unsaved values are tried, the stored key is kept, nothing is saved
    assert seen == {"provider": "anthropic", "model": "c", "key": "k"} and settings.llm_provider == "ollama"

    def failing(prompt, cfg):
        raise llm.LLMError("Anthropic answered HTTP 401: invalid x-api-key")

    monkeypatch.setattr(llm, "complete", failing)
    r = client.post("/api/settings/llm/test", json={"values": {}})
    assert r.status_code == 502 and "invalid x-api-key" in r.json()["detail"]

    def unreachable(cfg):
        raise llm.requests.ConnectionError("HTTPConnectionPool(host='x', port=9): Max retries exceeded")

    monkeypatch.setattr(llm, "list_models", unreachable)
    r = client.post("/api/settings/llm/models", json={"values": {}})
    assert r.status_code == 502 and "can't reach" in r.json()["detail"] and "Pool" not in r.json()["detail"]

    monkeypatch.setattr(llm, "list_models", lambda cfg: ["a", "b"])
    r = client.post("/api/settings/llm/models", json={"values": {"llm_model": ""}})
    assert r.json()["models"] == ["a", "b"]
