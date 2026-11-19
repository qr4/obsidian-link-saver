from saver import llm
from saver.config import settings


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"response": "{}"}


def test_requests_set_the_context_window(monkeypatch):
    """Filing sends the whole Areas tree; Ollama's default context can be far
    smaller and silently cuts the prompt."""
    sent = {}
    monkeypatch.setattr(llm.requests, "post", lambda url, json, timeout: sent.update(json) or FakeResponse())
    llm.call_raw("hello")
    assert sent["options"]["num_ctx"] == settings.ollama_num_ctx == 32768

    monkeypatch.setattr(settings, "ollama_num_ctx", 0)
    llm.call_raw("hello")
    assert "num_ctx" not in sent["options"]
