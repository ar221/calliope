"""Full SillyTavern persona catalog integration coverage."""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
sillytavern = importlib.import_module("calliope_server.sillytavern")


def _write_settings(path):
    path.write_text(json.dumps({
        "power_user": {
            "personas": {
                "ayaz.png": "Ayaz Rashid",
                "alex.png": "Alex",
            },
            "persona_descriptions": {
                "ayaz.png": {"description": "Terse, controlled, first-person voice."},
                "alex.png": {"description": "{{user}} is warm, playful, and highly physical in prose."},
            },
        },
        "api_key": "must-not-leak",
    }), encoding="utf-8")


def test_discover_personas_includes_full_st_list(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    local = tmp_path / "local"
    local.mkdir()
    (local / "nightshade.md").write_text("# Nightshade\nPrivate local persona.", encoding="utf-8")
    monkeypatch.setattr(sillytavern.config, "ST_SETTINGS_FILE", settings)
    monkeypatch.setattr(sillytavern.config, "PERSONAS_DIR", local)

    personas = sillytavern.discover_personas()
    assert {p["id"] for p in personas} == {"ayaz.png", "alex.png", "nightshade"}
    assert all("description" not in p for p in personas)
    assert "must-not-leak" not in json.dumps(personas)


def test_st_persona_description_shapes_voice_and_full_card(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr(sillytavern.config, "ST_SETTINGS_FILE", settings)
    monkeypatch.setattr(sillytavern.config, "PERSONAS_DIR", local)

    voice = sillytavern.load_persona_voice("alex.png")
    assert "SillyTavern persona" in voice
    assert "Alex is warm, playful" in voice
    assert "{{user}}" not in voice
    full = sillytavern.load_persona_full("alex.png")
    assert full["name"] == "Alex"
    assert "highly physical in prose" in full["description"]