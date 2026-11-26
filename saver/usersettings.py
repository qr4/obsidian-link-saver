"""Settings changed from the web UI's Settings tab.

Environment variables stay the base; what is changed in the UI is stored in
CONFIG_DIR/settings.json and wins over them. A value set back to what the
environment says is dropped from the file, so a later change to the
environment takes effect again. Only the fields in FIELDS can be changed
this way - paths, the API token and the like stay environment-only.

The API key is a secret: the file is written with mode 0600 and the key is
never returned by the API, only whether one is set.
"""
import dataclasses
import json
import os
import tempfile
import threading
from pathlib import Path

from .config import DEFAULT_LLM_URLS, LLM_PROVIDERS, Settings, settings
from .jobctx import log

WHISPER_DEVICES = ("auto", "cuda", "cpu")
WHISPER_COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8", "int8_bfloat16", "bfloat16", "float32")
# Suggestions for the UI; any faster-whisper model name or a local path works.
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo",
                  "distil-large-v3", "tiny.en", "base.en", "small.en", "medium.en")

# name -> (kind, options)
FIELDS = {
    "llm_provider": ("choice", {"choices": LLM_PROVIDERS}),
    "llm_url": ("url", {}),
    "llm_model": ("str", {"required": True}),
    "llm_api_key": ("secret", {}),
    "llm_temperature": ("optfloat", {"min": 0.0, "max": 2.0}),
    "llm_timeout": ("int", {"min": 1, "max": 3600}),
    "ollama_think": ("bool", {}),
    "ollama_num_ctx": ("int", {"min": 0, "max": 2_000_000}),
    "whisper_enabled": ("bool", {}),
    "whisper_model": ("str", {"required": True}),
    "whisper_device": ("choice", {"choices": WHISPER_DEVICES}),
    "whisper_compute_type": ("choice", {"choices": WHISPER_COMPUTE_TYPES}),
    "whisper_batch_size": ("int", {"min": 1, "max": 128}),
    "whisper_unload_after": ("int", {"min": 0, "max": 7 * 86400}),
}

_lock = threading.Lock()


class SettingsError(ValueError):
    pass


def coerce(name: str, value):
    """The value as the setting's type, or SettingsError."""
    if name not in FIELDS:
        raise SettingsError(f"{name} can't be changed here")
    kind, opts = FIELDS[name]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            return value.strip().lower() in ("1", "true", "yes", "on")
        raise SettingsError(f"{name}: expected true or false")
    if kind in ("int", "optfloat"):
        if kind == "optfloat" and (value is None or (isinstance(value, str) and value.strip() == "")):
            return None
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{name}: expected a number") from None
        if isinstance(value, bool) or (kind == "int" and isinstance(value, float) and value != int(value)):
            raise SettingsError(f"{name}: expected a whole number")
        if not opts["min"] <= number <= opts["max"]:
            raise SettingsError(f"{name}: must be between {opts['min']} and {opts['max']}")
        return number
    if not isinstance(value, str):
        raise SettingsError(f"{name}: expected text")
    value = value.strip()
    if kind == "choice":
        value = value.lower()
        if value not in opts["choices"]:
            raise SettingsError(f"{name}: expected one of {', '.join(opts['choices'])}")
    elif kind == "url":
        value = value.rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise SettingsError(f"{name}: expected an http:// or https:// URL, or nothing for the default")
    elif opts.get("required") and not value:
        raise SettingsError(f"{name}: can't be empty")
    return value


def _env_defaults() -> Settings:
    """What the settings would be without settings.json."""
    return Settings()


def _read_file() -> dict:
    path = settings.user_settings_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"WARNING: ignoring unreadable {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(overrides: dict):
    path = settings.user_settings_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _valid_overrides(raw: dict) -> dict:
    overrides = {}
    for name, value in raw.items():
        try:
            overrides[name] = coerce(name, value)
        except SettingsError as e:
            log(f"WARNING: {settings.user_settings_path}: ignoring {e}")
    return overrides


def load() -> dict:
    """Apply settings.json on top of the environment (at startup)."""
    with _lock:
        overrides = _valid_overrides(_read_file())
        settings.update(**overrides)
        return overrides


def save(values: dict, reset=()) -> dict:
    """Change settings: `values` maps names to new values (an empty API key
    keeps the stored one), `reset` names settings to return to the
    environment's value. Validates everything before changing anything."""
    coerced = {}
    for name, value in (values or {}).items():
        if FIELDS.get(name, ("",))[0] == "secret" and (value is None or value == ""):
            continue
        coerced[name] = coerce(name, value)
    for name in reset:
        if name not in FIELDS:
            raise SettingsError(f"{name} can't be changed here")
    with _lock:
        env = _env_defaults()
        overrides = _valid_overrides(_read_file())
        for name in reset:
            overrides.pop(name, None)
        for name, value in coerced.items():
            if value == getattr(env, name):
                overrides.pop(name, None)
            else:
                overrides[name] = value
        _write_file(overrides)
        settings.update(**{name: overrides.get(name, getattr(env, name)) for name in {*coerced, *reset}})
    return describe()


def candidate(values: dict) -> Settings:
    """A copy of the live settings with unsaved form values applied, to try
    them (list models, test) before saving. Invalid values raise."""
    changes = {}
    for name, value in (values or {}).items():
        if name not in FIELDS or not name.startswith(("llm_", "ollama_")):
            continue
        if value is None or value == "":
            # keep the stored key; listing models works before a model is chosen
            if FIELDS[name][0] == "secret" or FIELDS[name][1].get("required"):
                continue
        changes[name] = coerce(name, value)
    return dataclasses.replace(settings, **changes)


def describe() -> dict:
    """Current values, which are set in the UI, and what the environment
    says - never the API key itself."""
    env = _env_defaults()
    overridden = set(_read_file()) & set(FIELDS)
    fields = {}
    for name, (kind, _opts) in FIELDS.items():
        value, env_value = getattr(settings, name), getattr(env, name)
        if kind == "secret":
            value, env_value = bool(value), bool(env_value)
        fields[name] = {"value": value, "env": env_value, "overridden": name in overridden}
    return {
        "fields": fields,
        "llm_base_url": settings.llm_base_url,
        # what an empty llm_url means for each provider
        "default_urls": {"ollama": settings.ollama_url, **DEFAULT_LLM_URLS},
        "choices": {
            "llm_provider": list(LLM_PROVIDERS),
            "whisper_device": list(WHISPER_DEVICES),
            "whisper_compute_type": list(WHISPER_COMPUTE_TYPES),
            "whisper_model": list(WHISPER_MODELS),
        },
        "path": str(settings.user_settings_path),
    }
