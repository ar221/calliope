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


@pytest.mark.parametrize("voice,text,lang", [
    ("af_heart", "The keeper climbed the stairs.", "en-us"),
    # Same language, regional accent: the voice's variant wins.
    ("bf_emma", "The keeper climbed the stairs.", "en-gb"),
    # English text read by a non-English voice stays on the content language.
    ("ff_siwis", "The keeper climbed the stairs and she was not afraid.", "en-us"),
    ("jf_alpha", "The keeper climbed the stairs.", "en-us"),
    ("zf_xiaobei", "The keeper climbed the stairs.", "en-us"),
    ("ef_dora", "hello", "en-us"),
    # Text in the voice's own language uses it.
    ("ff_siwis", "Le gardien est dans la tour avec nous.", "fr-fr"),
    ("ef_dora", "El guardián está en la torre con los otros.", "es"),
    ("jf_alpha", "こんにちは、灯台守です。", "ja"),
    ("zf_xiaobei", "你好，我是灯塔看守人。", "cmn"),
])
def test_g2p_language_follows_the_text_not_just_the_voice(server, voice, text, lang):
    server.module._voice_ids = [voice]
    server.fake.tokenizer.unsupported = set()
    status, _ = server.post({"text": text, "voice": voice})
    assert status == 200
    assert {c["lang"] for c in server.fake.calls} == {lang}


def test_unavailable_language_falls_back_to_en_us(server, caplog):
    caplog.set_level("WARNING", logger="kokoro-server")
    for _ in range(2):
        status, _ = server.post({"text": "こんにちは", "voice": "jf_alpha"})
        assert status == 200
    assert {c["lang"] for c in server.fake.calls} == {"en-us"}
    warnings = [r for r in caplog.records if "unavailable for G2P" in r.getMessage()]
    assert len(warnings) == 1


def test_phoneme_overflow_splits_the_chunk_instead_of_failing(server):
    """Kokoro raises IndexError when a chunk's phoneme count exceeds its limit."""
    original = server.fake.create

    def create(text, voice, speed, lang):
        if len(text) > 12:
            raise IndexError("index 510 is out of bounds")
        return original(text, voice, speed, lang)

    server.fake.create = create
    text = "one two three four five six seven eight"
    status, _ = server.post({"text": text, "voice": "af_heart"})
    assert status == 200
    assert " ".join(c["text"] for c in server.fake.calls) == text


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
