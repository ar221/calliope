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
                "user.png": "Test User",
                "writer.png": "Test Writer",
            },
            "persona_descriptions": {
                "user.png": {"description": "Concise, controlled, first-person voice."},
                "writer.png": {"description": "{{user}} is warm, playful, and vivid in prose."},
            },
        },
        "api_key": "must-not-leak",
    }), encoding="utf-8")


def test_discover_personas_includes_full_st_list(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    local = tmp_path / "local"
    local.mkdir()
    (local / "local-persona.md").write_text("# Local Persona\nPrivate local persona.", encoding="utf-8")
    monkeypatch.setattr(sillytavern.config, "ST_SETTINGS_FILE", settings)
    monkeypatch.setattr(sillytavern.config, "PERSONAS_DIR", local)

    personas = sillytavern.discover_personas()
    assert {p["id"] for p in personas} == {"user.png", "writer.png", "local-persona"}
    assert all("description" not in p for p in personas)
    assert "must-not-leak" not in json.dumps(personas)


def test_st_persona_description_shapes_voice_and_full_card(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr(sillytavern.config, "ST_SETTINGS_FILE", settings)
    monkeypatch.setattr(sillytavern.config, "PERSONAS_DIR", local)

    voice = sillytavern.load_persona_voice("writer.png")
    assert "SillyTavern persona" in voice
    assert "Test Writer is warm, playful" in voice
    assert "{{user}}" not in voice
    full = sillytavern.load_persona_full("writer.png")
    assert full["name"] == "Test Writer"
    assert "vivid in prose" in full["description"]