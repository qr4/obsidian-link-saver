"""client/obsidian-save, loaded as a module."""
import importlib.machinery
import importlib.util
import urllib.error
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "client" / "obsidian-save"


def load_client():
    loader = importlib.machinery.SourceFileLoader("obsidian_save_client", str(CLIENT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_waiting_survives_a_server_restart(monkeypatch):
    """An update restarts the service while a keybinding save waits for its
    job; one failed poll must not end the client before the job is done."""
    client = load_client()
    answers = [urllib.error.URLError("connection refused"), {"status": "running"},
               {"status": "done", "result": "Saved"}]

    def fake_request(method, path, *a, **kw):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert client.wait_for([7]) == {7: {"status": "done", "result": "Saved"}}
