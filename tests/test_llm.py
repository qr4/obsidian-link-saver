import dataclasses

import pytest

from saver import llm
from saver.config import settings


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self.body


@pytest.fixture
def sent(monkeypatch):
    """Captures the last request; answers with what `sent["reply"]` holds."""
    box = {"reply": {"response": "{}"}}

    def post(url, json, headers, timeout):
        box.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(box["reply"], box.get("status", 200))

    monkeypatch.setattr(llm.requests, "post", post)
    return box


def cfg(**overrides):
    return dataclasses.replace(settings, **overrides)


def test_requests_set_the_context_window(sent, monkeypatch):
    """Filing sends the whole Areas tree; Ollama's default context can be far
    smaller and silently cuts the prompt."""
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    llm.call_raw("hello")
    assert sent["json"]["options"]["num_ctx"] == settings.ollama_num_ctx

    monkeypatch.setattr(settings, "ollama_num_ctx", 0)
    llm.call_raw("hello")
    assert "num_ctx" not in sent["json"]["options"]


def test_ollama_request(sent):
    sent["reply"] = {"response": "answer"}
    out = llm.complete("hi", cfg(llm_provider="ollama", llm_url="", ollama_url="http://o:11434",
                                 llm_model="m", llm_temperature=0.2, llm_api_key=""))
    assert out == "answer"
    assert sent["url"] == "http://o:11434/api/generate"
    assert sent["json"]["model"] == "m" and sent["json"]["options"]["temperature"] == 0.2
    assert sent["headers"] == {}


def test_openai_compatible_request(sent):
    sent["reply"] = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    out = llm.complete("hi", cfg(llm_provider="openai", llm_url="http://lm:1234/v1/", llm_model="local",
                                 llm_api_key="sk-x", llm_temperature=None))
    assert out == '{"ok": true}'
    assert sent["url"] == "http://lm:1234/v1/chat/completions"
    assert sent["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in sent["json"]
    assert sent["headers"] == {"Authorization": "Bearer sk-x"}


def test_openai_default_url(sent):
    sent["reply"] = {"choices": [{"message": {"content": "x"}}]}
    llm.complete("hi", cfg(llm_provider="openai", llm_url="", llm_model="gpt"))
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"


def test_anthropic_request(sent):
    sent["reply"] = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "a"},
                                 {"type": "text", "text": "b"}]}
    for url in ("", "https://api.anthropic.com/v1"):
        out = llm.complete("hi", cfg(llm_provider="anthropic", llm_url=url, llm_model="claude",
                                     llm_api_key="k", llm_temperature=0.2))
        assert out == "ab"
        assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "k" and sent["headers"]["anthropic-version"]
    assert sent["json"]["max_tokens"] > 0 and sent["json"]["temperature"] == 0.2


def test_errors_keep_the_servers_message(sent):
    sent["status"], sent["reply"] = 404, {"error": {"message": "model 'nope' not found"}}
    with pytest.raises(llm.LLMError, match="HTTP 404: model 'nope' not found"):
        llm.complete("hi", cfg(llm_provider="openai", llm_model="nope"))


def test_unknown_provider():
    with pytest.raises(llm.LLMError, match="unknown LLM provider"):
        llm.complete("hi", cfg(llm_provider="bogus"))


def test_list_models(monkeypatch):
    def get(url, headers, timeout):
        if url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "b:7b"}, {"name": "a:1b"}]})
        return FakeResponse({"data": [{"id": "gpt-x"}, {"id": None}]})

    monkeypatch.setattr(llm.requests, "get", get)
    assert llm.list_models(cfg(llm_provider="ollama")) == ["a:1b", "b:7b"]
    assert llm.list_models(cfg(llm_provider="openai")) == ["gpt-x"]
