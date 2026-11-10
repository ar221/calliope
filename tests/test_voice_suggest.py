"""Voice-suggestion scorer (`_suggest_voices`) contract tests.

Defends the casting behaviour a SillyTavern user observes from
POST /tts/voices/suggest:

- word-token matching (punctuation-adjacent gender/accent words count;
  substrings inside other words do not trigger tones or accents)
- an explicit nationality mention outranks coincidental tone adjectives
  on the wrong accent pack
- catalog age/energy metadata influences ranking
- the pronoun "us" never sets an accent preference

Uses the real shipped voice_catalog.json — these are casting contracts
against the actual catalog, same architecture as test_tts.py.
"""
from __future__ import annotations

import pytest

from conftest import load_wrapper


@pytest.fixture(scope="module")
def mod():
    return load_wrapper("calliope_voice_suggest")


def suggest(mod, name, description, personality="", n=3):
    return mod._suggest_voices(name, description, personality, [], {}, "", n=n)


# ─── Gender detection survives punctuation ───────────────────


def test_gender_word_followed_by_comma_counts(mod):
    out = suggest(mod, "Priya", "An Indian woman, gentle and soft-spoken.")
    catalog = mod._load_voice_catalog()
    assert catalog[out[0]["voice"]]["gender"] == "feminine"


# ─── Tone keywords are tokens, not substrings ────────────────


def test_no_tone_from_substring_inside_other_word(mod):
    # "scolded" must not trigger the "cold" tone: identical masc prompts with
    # a neutral verb swapped in must produce the identical ranking.
    ranked_scolded = [v["voice"] for v in suggest(mod, "Sam", "He scolded them harshly.")]
    ranked_praised = [v["voice"] for v in suggest(mod, "Sam", "He praised them harshly.")]
    assert ranked_scolded == ranked_praised


# ─── Explicit nationality dominates wrong-accent tone stacks ─


def test_explicit_spanish_male_tops_spanish_voices(mod):
    # "calm" + "deep-voiced" fit jm_kumo/zm_yunxia perfectly, but the user
    # said Mexican: the Spanish pack must win.
    out = suggest(mod, "Diego", "A Mexican man, calm and deep-voiced.", "stoic")
    assert out[0]["voice"] in ("em_alex", "em_santa")


def test_explicit_spanish_female_tops_ef_dora(mod):
    # "young bubbly girl" fits af_sky better on tone/age/energy alone; the
    # explicit Spain mention must still put the sole Spanish female first.
    out = suggest(mod, "Elena", "Señorita from Spain. She is a young, bubbly girl.", "sassy, playful")
    assert out[0]["voice"] == "ef_dora"


# ─── Pronoun "us" is not an accent signal ────────────────────


def test_pronoun_us_does_not_change_casting(mod):
    ranked_us = [v["voice"] for v in suggest(mod, "Sam", "He told us his story.")]
    ranked_them = [v["voice"] for v in suggest(mod, "Sam", "He told them his story.")]
    assert ranked_us == ranked_them


# ─── Catalog age/energy metadata influences ranking ──────────


def test_elderly_british_gentleman_gets_mature_british_voice(mod):
    out = suggest(mod, "Barnaby", "An elderly British gentleman, a wise old professor from London.")
    catalog = mod._load_voice_catalog()
    top = catalog[out[0]["voice"]]
    assert top["accent"] == "british"
    assert top["gender"] == "masculine"
    assert top["age"] == "mature"


def test_young_high_energy_prompt_prefers_young_adult_voice(mod):
    out = suggest(mod, "Pip", "She is a young, bubbly, energetic girl.")
    catalog = mod._load_voice_catalog()
    assert catalog[out[0]["voice"]]["age"] == "young adult"
    assert catalog[out[0]["voice"]]["energy"] == "high"
