"""Deterministic RP dictation evaluation helpers for Calliope.

MVP-27 intentionally stays local-only: no model judge, no cloud calls, and no
private transcript corpus. Fixtures can exercise invariant checks against
synthetic text before prompt changes ship.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class RPEvalCase:
    id: str
    raw: str
    output: str
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    speaker: str = "persona"
    addressee: str = ""
    pov: str = "first_person"
    max_expansion_ratio: float = 2.8
    latency_ms: int | None = None
    privacy_path: str = "synthetic"


@dataclass
class RPEvalResult:
    id: str
    passed: bool
    flags: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


# Han/kana are written without spaces, so each character counts as a word;
# every other script counts whitespace/punctuation-delimited letter runs.
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_WORD_RE = re.compile(rf"[{_CJK}]|[^\W{_CJK}]+(?:'[^\W{_CJK}]+)*")
_FIRST_PERSON_RE = re.compile(r"\b(?:i|me|my|mine|myself)\b", re.IGNORECASE)
_THIRD_PERSON_RE = re.compile(r"\b(?:he|she|they|him|her|them|his|their)\b", re.IGNORECASE)
_DIALOGUE_RE = re.compile(r'"[^"]*"|“[^”]*”')


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _is_mixed_pov(case: "RPEvalCase") -> bool:
    """First-person dictation narrated (partly) in third person.

    Third-person pronouns for OTHER characters are normal first-person
    prose ("I watch as she laughs"). The speaker drifts out of first person
    when the narration names them as a subject, or when first-person input
    comes back with no first-person narration at all. Quoted dialogue is
    ignored either way.
    """
    narration = _DIALOGUE_RE.sub(" ", case.output)
    speaker = case.speaker.strip()
    if speaker and speaker != "persona" and re.search(
        rf"\b{re.escape(speaker)}\s+(?:is|was|has|\w+(?:s|ed))\b", narration, re.IGNORECASE,
    ):
        return True
    return bool(
        _FIRST_PERSON_RE.search(_DIALOGUE_RE.sub(" ", case.raw))
        and not _FIRST_PERSON_RE.search(narration)
        and _THIRD_PERSON_RE.search(narration)
    )


def _contains(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def score_case(case: RPEvalCase) -> RPEvalResult:
    raw_words = _words(case.raw)
    out_words = _words(case.output)
    flags: list[str] = []

    for term in case.required_terms:
        if not _contains(case.output, term):
            flags.append(f"missing_required:{term}")
    for term in case.forbidden_terms:
        if _contains(case.output, term):
            flags.append(f"forbidden_term:{term}")

    if case.addressee and re.search(rf"\b{re.escape(case.addressee)}\s*[:：]", case.output, re.I):
        flags.append("other_character_dialogue")

    if case.pov == "first_person" and _is_mixed_pov(case):
        flags.append("mixed_pov")

    expansion_ratio = len(out_words) / max(1, len(raw_words))
    if expansion_ratio > case.max_expansion_ratio:
        flags.append("over_expanded")

    latency_bucket = "unknown"
    if case.latency_ms is not None:
        latency_bucket = "fast" if case.latency_ms < 1500 else "ok" if case.latency_ms < 4000 else "slow"

    if case.privacy_path not in {"synthetic", "local_fixture", "redacted"}:
        flags.append("unsafe_privacy_path")

    return RPEvalResult(
        id=case.id,
        passed=not flags,
        flags=flags,
        metrics={
            "raw_words": len(raw_words),
            "output_words": len(out_words),
            "expansion_ratio": round(expansion_ratio, 2),
            "latency_bucket": latency_bucket,
            "privacy_path": case.privacy_path,
        },
    )


def score_cases(cases: Iterable[RPEvalCase]) -> list[RPEvalResult]:
    return [score_case(case) for case in cases]


def summarize_results(results: Iterable[RPEvalResult]) -> dict:
    items = list(results)
    failed = [item for item in items if not item.passed]
    return {
        "total": len(items),
        "passed": len(items) - len(failed),
        "failed": len(failed),
        "flags": sorted({flag for item in failed for flag in item.flags}),
    }
