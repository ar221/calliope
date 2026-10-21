"""scripts/kokoro-server.py request handling, against a fake Kokoro model.

The real script imports `kokoro_onnx` (only present in the TTS venv); a stub
module stands in so the HTTP handler and synthesis plumbing run unchanged on
a loopback port 0 server.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import pathlib
import sys
import threading
import types
from http.server import ThreadingHTTPServer

import pytest

np = pytest.importorskip("numpy")

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "kokoro-server.py"


class FakeTokenizer:
    def __init__(self, unsupported: set[str]):
        self.unsupported = unsupported

    def phonemize(self, text, lang="en-us", norm=True):
        if lang in self.unsupported:
            raise RuntimeError(f"language {lang!r} is not supported")
        return "a"


class FakeKokoro:
    def __init__(self, unsupported: set[str] = frozenset()):
        self.tokenizer = FakeTokenizer(set(unsupported))
        self.calls: list[dict] = []

    def create(self, text, voice, speed, lang):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return np.full(240, 0.1, dtype=np.float32), 24000

    def get_voices(self):
        return ["af_heart", "bf_emma", "ff_siwis", "jf_alpha", "zf_xiaobei"]


def _load_script(monkeypatch, env: dict[str, str] | None = None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    stub = types.ModuleType("kokoro_onnx")
    stub.Kokoro = FakeKokoro
    monkeypatch.setitem(sys.modules, "kokoro_onnx", stub)
    spec = importlib.util.spec_from_file_location("kokoro_server_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(monkeypatch):
    ks = _load_script(monkeypatch)
    fake = FakeKokoro(unsupported={"ja"})
    ks._kokoro = fake
    ks._voice_ids = sorted(fake.get_voices())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ks.KokoroHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(payload: dict) -> tuple[int, bytes]:
        body = json.dumps(payload).encode()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/synthesize", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    yield types.SimpleNamespace(module=ks, fake=fake, post=post)
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("speed", [0, -1, 0.0])
def test_non_positive_speed_is_rejected(server, speed):
    status, body = server.post({"text": "hello", "voice": "af_heart", "speed": speed})
    assert status == 400
    assert json.loads(body)["code"] == "bad_input"
    assert server.fake.calls == []


def test_missing_speed_defaults_to_one(server):
    status, body = server.post({"text": "hello", "voice": "af_heart"})
    assert status == 200
    assert body[:4] == b"RIFF"
    assert server.fake.calls[0]["speed"] == 1.0


@pytest.mark.parametrize("voice,lang", [
    ("af_heart", "en-us"),
    ("bf_emma", "en-gb"),
    ("ff_siwis", "fr-fr"),
    ("zf_xiaobei", "cmn"),
])
def test_voice_prefix_selects_language(server, voice, lang):
    status, _ = server.post({"text": "hello", "voice": voice})
    assert status == 200
    assert {c["lang"] for c in server.fake.calls} == {lang}


def test_unavailable_language_falls_back_to_en_us(server, caplog):
    caplog.set_level("WARNING", logger="kokoro-server")
    for _ in range(2):
        status, _ = server.post({"text": "hello", "voice": "jf_alpha"})
        assert status == 200
    assert {c["lang"] for c in server.fake.calls} == {"en-us"}
    warnings = [r for r in caplog.records if "unavailable for G2P" in r.getMessage()]
    assert len(warnings) == 1


def test_lang_mapping_table(monkeypatch):
    ks = _load_script(monkeypatch, {"KOKORO_LANG": "en-gb"})
    expected = {
        "af_heart": "en-us", "bm_george": "en-gb", "ef_dora": "es",
        "ff_siwis": "fr-fr", "hf_alpha": "hi", "if_sara": "it",
        "jm_kumo": "ja", "pf_dora": "pt-br", "zm_yunxi": "cmn",
    }
    assert {v: ks._lang_for_voice(v) for v in expected} == expected
    # Unmapped IDs use KOKORO_LANG.
    assert ks._lang_for_voice("custom") == "en-gb"
    assert ks._lang_for_voice("xf_new") == "en-gb"
