import json
import os

import pytest

from saver import cookies

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "# This is a generated file! Do not edit.\n\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tPREF\tf6=40000000\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tsecret-value\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1000\tOLD\tx\n"
    ".google.com\tTRUE\t/\tFALSE\t0\tNID\n"          # 6 fields: empty value, trailing tab dropped
    "this line is garbage\n"
)


def test_normalize_keeps_cookies_and_adds_header():
    out = cookies.normalize(NETSCAPE.replace("# Netscape HTTP Cookie File\n", ""))
    assert out.startswith(cookies.HEADER + "\n")
    parsed, bad = cookies.parse_netscape(out)
    assert [c["name"] for c in parsed] == ["PREF", "__Secure-3PSID", "OLD", "NID"]
    assert bad == 0 and "garbage" not in out


def test_rejects_non_cookie_files():
    for text in ("", "hello world", "<html></html>", "[]"):
        with pytest.raises(cookies.CookieError):
            cookies.normalize(text)


def test_cookie_editor_json_is_converted():
    data = [
        {"domain": ".youtube.com", "hostOnly": False, "name": "SAPISID", "value": "v", "path": "/",
         "secure": True, "httpOnly": False, "session": False, "expirationDate": 1893456000.5},
        {"domain": "www.youtube.com", "hostOnly": True, "name": "ST", "value": "w", "path": "/",
         "secure": False, "httpOnly": True, "session": True},
    ]
    out = cookies.normalize(json.dumps(data))
    lines = out.splitlines()
    assert lines[0] == cookies.HEADER
    assert lines[1] == ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tv"
    assert lines[2] == "#HttpOnly_www.youtube.com\tFALSE\t/\tFALSE\t0\tST\tw"


def test_save_status_delete(env):
    status = cookies.save(NETSCAPE)
    path = cookies.target_path()
    assert path == env["config"] / "cookies.txt"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert status["count"] == 4 and status["expired"] == 1 and status["youtube"] and status["logged_in"]
    assert status["auth_expires"] == 1893456000 and status["valid_header"]
    assert "secret-value" not in json.dumps(status)
    assert cookies.delete() is True and cookies.status() == {"present": False, "path": str(path)}


def test_stored_file_loads_in_yt_dlp(env):
    yt_cookies = pytest.importorskip("yt_dlp.cookies")
    cookies.save(NETSCAPE.replace("# Netscape HTTP Cookie File\n", ""))
    jar = yt_cookies.YoutubeDLCookieJar(str(cookies.target_path()))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert {c.name for c in jar} == {"PREF", "__Secure-3PSID", "OLD", "NID"}


def test_cookie_api(env):
    from fastapi.testclient import TestClient
    from saver.app import create_app

    client = TestClient(create_app(run_lifecycle=False))
    assert client.get("/api/cookies").json()["present"] is False
    bad = client.post("/api/cookies", data={"text": "not cookies"})
    assert bad.status_code == 400 and "no cookies found" in bad.json()["detail"]
    ok = client.post("/api/cookies", files={"file": ("cookies.txt", NETSCAPE.encode(), "text/plain")}).json()
    assert ok["count"] == 4
    assert client.get("/api/state").json()["config"]["cookies"]["logged_in"] is True
    assert client.post("/api/cookies", data={"text": NETSCAPE}).json()["count"] == 4
    assert client.delete("/api/cookies").json()["deleted"] is True
