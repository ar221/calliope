"""Phone PWA: an `OOC:` utterance keeps its marker through Regenerate.

Runs the real `regenerate()` / `withOocPrefix()` from the embedded phone
script under Node with stubbed DOM/fetch, so this exercises behaviour rather
than pinning source text.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_UI = ROOT / "server" / "calliope_server" / "web_ui.py"


def _regen_block() -> str:
    src = WEB_UI.read_text(encoding="utf-8")
    start = src.index("function withOocPrefix(text)")
    end = src.index("// ─── Result display", start)
    return src[start:end]


def _run(last_was_ooc: bool) -> dict:
    harness = """
      const calls = { fetch: [], shown: [] };
      const el = () => ({ disabled: false, classList: { add() {}, remove() {} }, textContent: '' });
      const regenBtn = el(), btn = el(), label = el();
      const setIcon = () => {}; const showError = (m) => { throw new Error(m); };
      const showToast = () => {}; const flashRawBadge = () => {}; const updateLogDisplay = () => {};
      const getContext = () => ''; const resolveRegenMode = () => 'rp_enhance';
      const currentFormatterModel = ''; const chatSource = 'manual';
      const selectedPersona = 'none', selectedCharacter = 'none', useRules = false, proseFormat = false;
      let lastUsedMode = 'grammar_clean';
      let lastRawText = 'brb two minutes';
      let lastWasOoc = __OOC__;
      const showResult = (text, meta) => calls.shown.push({ text, mode: meta.mode });
      globalThis.fetch = async (url, init) => {
        calls.fetch.push({ url, body: JSON.parse(init.body) });
        return { ok: true, json: async () => ({ text: 'be right back in two minutes', mode: JSON.parse(init.body).mode }) };
      };
      __BLOCK__
      await regenerate();
      console.log(JSON.stringify(calls));
    """
    script = harness.replace("__OOC__", "true" if last_was_ooc else "false").replace("__BLOCK__", _regen_block())
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_ooc_regen_stays_in_grammar_clean_and_keeps_prefix():
    calls = _run(last_was_ooc=True)
    assert calls["fetch"][0]["url"] == "/reformat"
    assert calls["fetch"][0]["body"]["mode"] == "grammar_clean"
    assert calls["shown"] == [{"text": "OOC: be right back in two minutes", "mode": "grammar_clean"}]


def test_non_ooc_regen_is_unchanged():
    calls = _run(last_was_ooc=False)
    assert calls["fetch"][0]["body"]["mode"] == "rp_enhance"
    assert calls["shown"][0]["text"] == "be right back in two minutes"
