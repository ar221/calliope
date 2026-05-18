from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from importlib.machinery import SourceFileLoader

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "rp_eval.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "rp_eval" / "cases.json"


def load_mod():
    loader = SourceFileLoader("rp_eval", str(SRC))
    spec = importlib.util.spec_from_loader("rp_eval", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["rp_eval"] = module
    spec.loader.exec_module(module)
    return module


def test_rp_eval_passes_clean_synthetic_case():
    mod = load_mod()
    case = mod.RPEvalCase(
        id="clean",
        raw="I step closer and tell Elara I missed her",
        output="*I step closer.* \"Elara, I missed you.\"",
        required_terms=("Elara", "missed"),
        forbidden_terms=("Elara:",),
        addressee="Elara",
        latency_ms=900,
    )

    result = mod.score_case(case)

    assert result.passed
    assert result.flags == []
    assert result.metrics["latency_bucket"] == "fast"
    assert result.metrics["privacy_path"] == "synthetic"


def test_rp_eval_flags_other_character_speech_and_over_expansion():
    mod = load_mod()
    case = mod.RPEvalCase(
        id="bad",
        raw="I nod",
        output="Elara: I know. *I nod and invent a storm, a ballroom, and a stranger at the door.*",
        forbidden_terms=("stranger at the door",),
        addressee="Elara",
        max_expansion_ratio=2.0,
        privacy_path="synthetic",
    )

    result = mod.score_case(case)

    assert not result.passed
    assert "other_character_dialogue" in result.flags
    assert "over_expanded" in result.flags
    assert "forbidden_term:stranger at the door" in result.flags


def test_rp_eval_fixture_summary_is_local_and_deterministic():
    mod = load_mod()
    raw_cases = json.loads(FIXTURES.read_text())
    cases = [
        mod.RPEvalCase(
            id=item["id"],
            raw=item["raw"],
            output=item["output"],
            required_terms=tuple(item.get("required_terms", ())),
            forbidden_terms=tuple(item.get("forbidden_terms", ())),
            addressee=item.get("addressee", ""),
            max_expansion_ratio=item.get("max_expansion_ratio", 2.8),
            latency_ms=item.get("latency_ms"),
            privacy_path=item.get("privacy_path", "synthetic"),
        )
        for item in raw_cases
    ]

    summary = mod.summarize_results(mod.score_cases(cases))

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert "over_expanded" in summary["flags"]
